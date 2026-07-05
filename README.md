# Sector Gap Analyzer

Tooling for identifying **likely-but-undiscovered star systems** in specific
Elite Dangerous procedurally-generated sectors, for exploration flight
planning. Given a sector's known (Spansh-reported) systems, the pipeline
infers plausible names for systems that probably exist but haven't been
scanned/submitted yet, then cross-checks each candidate against EDSM to
filter out anything already known.

This does **not** predict coordinates and cannot prove a candidate exists —
it only narrows an enormous naming space down to a short, explainable list
worth flying to check. See [`docs/gap-finder.md`](docs/gap-finder.md) for the
original design rationale (note: that document describes a more elaborate
scoring model than what's actually implemented — see
[Relationship to docs/gap-finder.md](#relationship-to-docsgap-findermd) below).

## How candidates are generated

Elite Dangerous procedural system names follow a `<Sector> <Subsector> <mass
code><number>` pattern, e.g. `Outopps FG-Y d1-23`. Within one "family"
(everything except the trailing number), Stellar Forge tends to allocate
numbers roughly in order, so gaps in the known numbers are informative:

- **Bracketed gaps** (`scripts/gap_full_export.py`) — a missing number that
  sits strictly between two *consecutive* known numbers in a family (e.g.
  known `...12, 14...` → candidate `13`). This is the strongest signal: the
  candidate is boxed in on both sides by confirmed systems. Gaps wider than
  `--max-bracket-width` (default 25) are skipped — a very wide unbracketed
  stretch is more likely a number Stellar Forge never allocated than a real
  missing system.
- **Backward extrapolation** (`scripts/gap_extrapolate_export.py --direction
  backward`) — numbers below a family's known minimum, down toward 0 (e.g.
  known minimum `d1-5` → candidates `d1-4, d1-3, d1-2, d1-1, d1-0`). Bounded
  by `--extend-depth` (default 5).
- **Forward extrapolation** (same script, `--direction forward`) — numbers
  above a family's known maximum, chain-extended only while EDSM keeps
  confirming each step. Not currently part of the standard run for this
  project (see below) but available if needed.

Every candidate is checked against the [EDSM](https://www.edsm.net/) API
(`GET /api-v1/system?systemName=...`) and only kept if EDSM has no record of
it — i.e. it's not just "missing from Spansh," it's missing everywhere we can
cheaply check.

## Pipeline

```
1. Source data (galaxy.json.gz)              — you supply this, see below
        │
        ▼
2. Extraction → data/sector_library/*.sqlite  — scripts/extract_sector_systems_to_sqlite.py
                                                 scripts/extract_multi_sector_to_sqlite.py (many sectors, one pass)
        │
        ▼
3. Candidate generation + EDSM validation     — scripts/gap_full_export.py            (bracketed gaps)
                                                 scripts/gap_extrapolate_export.py      (backward/forward)
        │
        ▼
4. Cross-sector aggregation → out/            — scripts/aggregate_gap_master_list.py
```

A separate, optional path (`scripts/sector_survey.py`) surveys an already-
extracted sector DB for high-value systems (Earth-like/Water/Ammonia worlds,
dense icy/metallic rings, biosignature counts), cross-checks claim status via
the Spansh search API, and looks for spatial clusters of interesting systems.
This is independent of the gap/extrapolation candidate pipeline.

### 1. Source data: the galaxy dump

Nothing in this repo ships with system data. You need a local copy of a full
[Spansh](https://spansh.co.uk/dumps) galaxy dump — **not** the "populated
systems only" dump; extraction needs every system, populated or not, so gaps
in procedural naming sequences can be detected.

This is a large file: tens to over a hundred GB compressed. It can be either:
- a JSON array document (`[ {...}, {...}, ... ]`, one object per line) — this
  is what Spansh's full dump uses, and it requires the `ijson` package to
  stream without loading the whole thing into memory, or
- JSON Lines (one bare JSON object per line, no enclosing `[...]`) — detected
  automatically either way (`detect_format()` in
  `scripts/extract_sector_systems_to_sqlite.py`).

Point extraction at your local copy with `--input`, or set the
`MFI_GALAXY_DUMP` environment variable so scripts default to it:

```bash
export MFI_GALAXY_DUMP="/path/to/galaxy.json.gz"
```

Because decompression + JSON parsing dominates the cost of a pass over this
file (not the sector filter itself), **prefer
`extract_multi_sector_to_sqlite.py` over running the single-sector extractor
repeatedly** whenever you need more than one sector — it streams the dump
once and routes matching systems to N separate output databases in the same
pass. As a reference point, a 113 GB compressed dump on typical hardware
extracted at ~6,000–7,000 systems/sec, i.e. roughly 4–5 hours for a full
single pass regardless of how many sectors you extract from it in that pass.

### 2. Extraction

Single sector:

```bash
python scripts/extract_sector_systems_to_sqlite.py \
    --input "/path/to/galaxy.json.gz" \
    --sector_prefix "Outopps"
```

Multiple sectors in one pass (recommended whenever extracting >1 sector):

```bash
python scripts/extract_multi_sector_to_sqlite.py \
    --input "/path/to/galaxy.json.gz" \
    --sector_prefix "Outopps" --sector_prefix "Oochost" --sector_prefix "Oesotl"
```

Both write to `data/sector_library/sector_<slug>.sqlite` by default (three
tables: `systems`, `bodies`, `rings`, plus a `raw_json` column on each for
anything not explicitly mapped). Use `--limit N` on either script to sample a
prefix of the dump first and gauge throughput before committing to a full run
on very large source files.

Sector-prefix matching requires a token boundary (the prefix must be the
whole system name or be followed by a space), so `"Oochost"` won't
accidentally match an unrelated system starting with those same letters.

### 3. Candidate generation + validation

Run per sector, against its extracted DB:

```bash
# Bracketed gaps (dry-run first to see volume before spending EDSM calls):
python scripts/gap_full_export.py --db data/sector_library/sector_outopps.sqlite \
    --sector "Outopps" --out-dir out --dry-run
python scripts/gap_full_export.py --db data/sector_library/sector_outopps.sqlite \
    --sector "Outopps" --out-dir out

# Backward extrapolation only (no forward chaining):
python scripts/gap_extrapolate_export.py --db data/sector_library/sector_outopps.sqlite \
    --sector "Outopps" --out-dir out --direction backward --dry-run
python scripts/gap_extrapolate_export.py --db data/sector_library/sector_outopps.sqlite \
    --sector "Outopps" --out-dir out --direction backward
```

Both scripts write CSV + Markdown per phase into `--out-dir` (use the same
`out/` directory for every sector so the aggregator in step 4 can find
everything). Both throttle EDSM lookups to **1 request/second** and cache
results (default: an `edsm_cache` table inside the sector DB itself, 7-day
TTL) so re-runs only pay for new candidates. For a sector of unknown size,
always run `--dry-run` first — candidate volume (and therefore validation
wall-clock time) can vary by orders of magnitude between sectors depending on
how sparse and wide their naming families are.

### 4. Aggregation

```bash
python scripts/aggregate_gap_master_list.py --out-dir out
```

Scans `out/` for every sector's `*_gap_full_validated.csv` and
`*_extrap_backward_validated.csv` / `*_extrap_forward_step*.csv`, keeps only
rows EDSM has no record of (`edsm_status == not_in_edsm`), tags each with its
sector and candidate type, and writes one combined
`master_gap_candidates.csv` + `.md` — grouped by sector, then candidate type,
then naming family. No composite "confidence score" is invented: ordering is
structural (bracketed gaps, then backward extrapolation by proximity to the
confirmed edge) because the underlying data doesn't support anything more
precise than that.

## Dependencies

No `requirements.txt` exists yet; install what you need directly:

- **Python 3.10+** (uses `from __future__ import annotations`, PEP 604 unions
  in a few places).
- **`ijson`** — required for extraction whenever the source dump is a JSON
  array document rather than JSON Lines (this is the case for Spansh's full
  dump). `pip install ijson`. Performance depends heavily on backend — check
  which one you have with `python -c "import ijson; print(ijson.backend)"`;
  `yajl2_c` (a compiled C backend) is dramatically faster than the pure-
  Python fallback.
- **`aiohttp`** — only needed by `scripts/sector_survey.py` and
  `scripts/pencil_sector_survey.py` for querying the Spansh systems-search
  API. Not required for extraction or for the gap/extrapolation scripts,
  which use the standard library `urllib` against EDSM.
- Everything else (`sqlite3`, `csv`, `json`, `argparse`, ...) is standard
  library.

## Repository layout

```
scripts/
  extract_sector_systems_to_sqlite.py   Single-sector extraction from a galaxy dump
  extract_multi_sector_to_sqlite.py     Multi-sector extraction in one pass over the dump
  gap_full_export.py                    Bracketed intra-sequence gap candidates + EDSM validation
  gap_extrapolate_export.py             Backward/forward extrapolation candidates + EDSM validation
  aggregate_gap_master_list.py          Merge per-sector CSVs into one master candidate list
  sector_survey.py                      High-value system survey for an extracted sector DB (independent of gap pipeline)
  pencil_sector_survey.py               One-off hardcoded survey script for the Pencil Sector (not general-purpose)
  offline_gap_lists.py                  NOT CURRENTLY FUNCTIONAL — see note below

data/sector_library/     Extracted per-sector SQLite DBs (gitignored: *.sqlite)
out/                     Generated candidate CSVs/Markdown reports (gitignored)
docs/gap-finder.md       Original design document / methodology notes
planner/, tests/         Leftover from a prior reorg — see note below
```

### A note on `planner/`, `tests/`, and `offline_gap_lists.py`

This repo was split out from a larger project (`edmfi-mfi`). `planner/`
(imported elsewhere as `planner_strategic`), `tests/test_strategic_planner.py`,
and `scripts/offline_gap_lists.py` all reference modules that don't exist in
this repo (`app.config`, `planner_strategic`) and are **not currently
runnable** here. The actively-used, self-contained pipeline is everything
listed under "Repository layout" above except those three. Treat `planner/`
and `tests/` as reference material from the source project rather than
working code, unless/until they're reconnected or rewritten.

### Relationship to `docs/gap-finder.md`

The design doc describes a two-tier methodology with computed confidence
scores (`density_score`, `bracket_score_norm`, `family_strength_score`, a
weighted `priority_score`, spatial neighborhood anchoring, etc.). The scripts
that actually exist today implement a simpler subset: bracketed-gap detection
with a bracket-width cap, and backward/forward extrapolation gated by EDSM
confirmation — no computed composite score, no spatial anchoring tier. Treat
the doc as background on the *reasoning* behind the approach, not as a spec
that matches the current code line-for-line.

## Output format reference

`gap_full_export.py` and `gap_extrapolate_export.py` write CSVs with a shared
schema so `aggregate_gap_master_list.py` can merge them:

| Column | Meaning |
|---|---|
| `system_name` | Candidate system name |
| `edsm_status` | `not_in_edsm` (kept), `in_edsm` (filtered out), `skipped` (dry-run) |
| `direction` | `bracketed_gap`, `backward`, or `forward` |
| `steps_from_edge` | For backward/forward: distance from the known edge (1 = adjacent) |
| `spansh_edge_number` | The known min (backward) or max (forward) the candidate extends from |
| `family` | Sector + subsector token, e.g. `Outopps FG-Y` |
| `subsector` | Just the subsector token, e.g. `FG-Y` |
| `mass_prefix` | The mass-code letter+digit, e.g. `d1` |
| `number` | The candidate's trailing number |

## Caveats

- Candidates are hypotheses, not confirmed systems. `not_in_edsm` means
  "unreported to Spansh or EDSM," which includes real-but-undiscovered
  systems, real-but-unsubmitted discoveries, *and* numbers Stellar Forge
  simply never used.
- Source data goes stale — Spansh dumps are periodic snapshots, and both
  Spansh and EDSM coverage grow over time as more commanders explore and
  submit data. Re-running validation against a fresh dump/cache periodically
  will change results.
- EDSM's 1 request/second throttle means validation time scales with
  candidate volume; always check with `--dry-run` before a full run on an
  unfamiliar sector.

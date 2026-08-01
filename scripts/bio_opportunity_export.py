#!/usr/bin/env python3
"""
bio_opportunity_export.py — Stale exobiology candidate finder.
=============================================================================
Surfaces bodies with detected biological signals whose signal data hasn't
been reconfirmed since Elite Dangerous: Odyssey's release (2021-05-19) --
the expansion that introduced on-foot landing, the Genetic Sampler, and the
Vista Genomics sample economy. A body whose `signals.updateTime` (in the
extracted sector DB's raw_json -- distinct from the body's overall
`updateTime`, which drifts forward from unrelated orbital-mechanics
recalculation and would give false negatives) predates Odyssey is good
evidence nobody has even re-scanned it since the sampling gameplay existed,
which is a reasonably strong -- not certain -- proxy for "probably still
unsampled." A commander could in principle land and sample from old data
without ever re-scanning; this tool can't see that case.

This is NOT a value estimator. Species -- the actual credit-value driver --
depends on the host body's star type/atmosphere/temperature via Frontier's
speciation rules, which this tool does not attempt to reproduce (that ruleset
changes with balance patches and is already maintained elsewhere, e.g. the
Elite Dangerous Fandom wiki, ed-dsn.net). Genus name is surfaced when known
so you can cross-reference those tools yourself.

Like the gap-analysis scripts, this surfaces hypotheses worth a personal
verification flight, not guarantees -- entirely offline, no EDSM/network
calls: everything needed is already in the extracted sector DB.

Usage:
  python scripts/bio_opportunity_export.py \\
      --db data/sector_library/sector_heart_sector.sqlite \\
      --sector "Heart Sector" --out-dir out

  # Use a different cutoff date (e.g. to test a different expansion/patch):
  python scripts/bio_opportunity_export.py \\
      --db data/sector_library/sector_heart_sector.sqlite \\
      --sector "Heart Sector" --out-dir out --cutoff-date 2023-04-11
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from scripts import gap_naming
    from scripts.extract_sector_systems_to_sqlite import sanitize_prefix
except ImportError:
    import gap_naming
    from extract_sector_systems_to_sqlite import sanitize_prefix

# Odyssey introduced landing, the Genetic Sampler, and the Vista Genomics
# sample economy -- bio signals last (re)confirmed before this date have
# never been checked under the mechanic that makes them worth anything.
ODYSSEY_RELEASE_DATE = "2021-05-19"

# Genus codex identifier -> human-readable name. Stable reference data (FDev
# rarely renames genera), unlike per-species credit values (which change with
# balance patches and depend on host-body conditions) -- deliberately not
# embedded here. Cross-referenced against the exact codex identifiers found
# in real extracted data against EDMC-ExploData's genus reference:
# https://github.com/Silarn/EDMC-ExploData
GENUS_NAMES: dict[str, str] = {
    "$Codex_Ent_Aleoids_Genus_Name;": "Aleoida",
    "$Codex_Ent_Bacterial_Genus_Name;": "Bacterium",
    "$Codex_Ent_Cactoid_Genus_Name;": "Cactoida",
    "$Codex_Ent_Clypeus_Genus_Name;": "Clypeus",
    "$Codex_Ent_Conchas_Genus_Name;": "Concha",
    "$Codex_Ent_Cone_Name;": "Bark Mound",
    "$Codex_Ent_Electricae_Genus_Name;": "Electricae",
    "$Codex_Ent_Fonticulus_Genus_Name;": "Fonticulua",
    "$Codex_Ent_Fumerolas_Genus_Name;": "Fumerola",
    "$Codex_Ent_Fungoids_Genus_Name;": "Fungoida",
    "$Codex_Ent_Ground_Struct_Ice_Name;": "Crystalline Shards",
    "$Codex_Ent_Osseus_Genus_Name;": "Osseus",
    "$Codex_Ent_Recepta_Genus_Name;": "Recepta",
    "$Codex_Ent_Brancae_Name;": "Brain Tree",
    "$Codex_Ent_Shrubs_Genus_Name;": "Frutexa",
    "$Codex_Ent_Sphere_Name;": "Anemone",
    "$Codex_Ent_Stratum_Genus_Name;": "Stratum",
    "$Codex_Ent_Tube_Name;": "Sinuous Tubers",
    "$Codex_Ent_Tubus_Genus_Name;": "Tubus",
    "$Codex_Ent_Tussocks_Genus_Name;": "Tussock",
    "$Codex_Ent_Vents_Name;": "Amphora Plant",
}


def _genus_label(codex_name: str) -> str:
    return GENUS_NAMES.get(codex_name, codex_name.strip("$;").replace("_", " "))


class Candidate:
    __slots__ = (
        "system_name", "body_name", "signal_count", "genus_names",
        "signals_updated", "body_updated",
    )

    def __init__(
        self,
        system_name: str,
        body_name: str,
        signal_count: int,
        genus_names: list[str],
        signals_updated: str,
        body_updated: str,
    ) -> None:
        self.system_name = system_name
        self.body_name = body_name
        self.signal_count = signal_count
        self.genus_names = genus_names
        self.signals_updated = signals_updated
        self.body_updated = body_updated


def _parse_timestamp(raw: str) -> Optional[datetime]:
    """Spansh-style timestamps look like '2020-08-31 14:41:24+00'."""
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def find_stale_bio_candidates(conn: sqlite3.Connection, cutoff_date: str) -> list[Candidate]:
    """Bodies with a Biological signal count > 0 whose signals.updateTime
    predates cutoff_date (default: Odyssey's release)."""
    cutoff = _parse_timestamp(cutoff_date + " 00:00:00")
    if cutoff is None:
        raise ValueError(f"Invalid cutoff date {cutoff_date!r}; expected YYYY-MM-DD")

    candidates: list[Candidate] = []
    rows = conn.execute(
        "SELECT system_name, body_name, raw_json FROM bodies WHERE raw_json LIKE '%Biological%'"
    ).fetchall()

    for system_name, body_name, raw_json in rows:
        try:
            data = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            continue

        signals_block = data.get("signals") or {}
        signal_counts = signals_block.get("signals") or {}
        bio_count = sum(v for k, v in signal_counts.items() if "Biological" in k)
        if bio_count <= 0:
            continue

        updated = _parse_timestamp(signals_block.get("updateTime", ""))
        if updated is None or updated >= cutoff:
            continue  # no timestamp, or reconfirmed since the cutoff -- not a candidate

        genus_names = [_genus_label(g) for g in signals_block.get("genuses", [])]
        candidates.append(Candidate(
            system_name=system_name,
            body_name=body_name,
            signal_count=bio_count,
            genus_names=genus_names,
            signals_updated=signals_block.get("updateTime", ""),
            body_updated=data.get("updateTime", ""),
        ))

    return candidates


def _location_sort_key(c: Candidate) -> tuple:
    return gap_naming.group_sort_key(c.system_name) + (c.body_name,)


def write_csv(out_path: Path, candidates: list[Candidate], cutoff_date: str) -> None:
    ordered = sorted(candidates, key=_location_sort_key)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "system_name", "body_name", "subsector", "mass_code", "boxel", "number",
            "biological_signal_count", "genus_names", "signals_last_reported", "cutoff_date",
        ])
        for c in ordered:
            subsector, mass_code, boxel, number, _ = gap_naming.group_sort_key(c.system_name)
            writer.writerow([
                c.system_name, c.body_name, subsector, mass_code, boxel, number,
                c.signal_count, "; ".join(c.genus_names), c.signals_updated, cutoff_date,
            ])
    print(f"  CSV: {out_path}  ({len(candidates)} rows)")


def write_markdown(
    out_path: Path,
    candidates: list[Candidate],
    sector: str,
    db_path: Path,
    cutoff_date: str,
    known_bio_bodies: int,
) -> None:
    ordered = sorted(candidates, key=_location_sort_key)
    genus_known = [c for c in ordered if c.genus_names]

    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# {sector} — Stale Exobiology Candidates\n\n")
        f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Source: `{db_path.name}`\n")
        f.write(f"Cutoff date: {cutoff_date} (Elite Dangerous: Odyssey release, unless overridden)\n")
        f.write(f"Bodies with any biological signal in this DB: {known_bio_bodies}\n")
        f.write(f"Candidates (signals not reconfirmed since cutoff): {len(candidates)}\n")
        f.write(f"  Of those, genus already identified (DSS done): {len(genus_known)}\n\n")
        f.write(
            "> These are hypotheses worth a personal verification flight, not guarantees --\n"
            "> a body whose biological signals haven't been reconfirmed since the cutoff date\n"
            "> is good evidence nobody has re-scanned it since Odyssey's sampling gameplay\n"
            "> existed, which is a reasonably strong (not certain) proxy for \"probably still\n"
            "> unsampled.\" This tool does not estimate credit value -- that depends on the\n"
            "> exact species, which depends on host-body conditions via Frontier's speciation\n"
            "> rules. Cross-reference genus names against a community value table (e.g. the\n"
            "> Elite Dangerous Fandom wiki, ed-dsn.net) before planning a dedicated trip.\n\n"
        )
        f.write("---\n\n")

        if not candidates:
            f.write("*No stale bio-signal candidates found in this sector.*\n")
            print(f"  MD:  {out_path}")
            return

        f.write("## Top candidates\n\n")
        f.write("Sorted by signal count, genus-identified first.\n\n")
        f.write("| System | Body | Signals | Genus | Last reported |\n")
        f.write("|--------|------|--------:|-------|----------------|\n")
        top = sorted(
            ordered, key=lambda c: (-len(c.genus_names), -c.signal_count, c.system_name)
        )[:15]
        for c in top:
            genus = ", ".join(c.genus_names) if c.genus_names else "*(unidentified -- FSS only)*"
            f.write(f"| {c.system_name} | {c.body_name} | {c.signal_count} | {genus} | {c.signals_updated} |\n")
        f.write("\n---\n\n")

        f.write("## Full list, grouped by location\n\n")
        by_subsector: dict[str, list[Candidate]] = defaultdict(list)
        for c in ordered:
            subsector = gap_naming.extract_subsector(c.system_name) or "(unparsed)"
            by_subsector[subsector].append(c)

        for subsector in sorted(by_subsector):
            group = by_subsector[subsector]
            f.write(f"### {sector} {subsector} ({len(group)})\n\n")
            for c in group:
                genus = ", ".join(c.genus_names) if c.genus_names else "unidentified, FSS only"
                f.write(
                    f"  - **{c.system_name}** / {c.body_name} -- "
                    f"{c.signal_count} signal(s), {genus} *(last reported {c.signals_updated})*\n"
                )
            f.write("\n")

    print(f"  MD:  {out_path}")


def run(
    db_path: Path,
    sector: str,
    out_dir: Path,
    cutoff_date: str = ODYSSEY_RELEASE_DATE,
) -> Path:
    sector = sector.strip()
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Bio Opportunity Export")
    print(f"  Source DB:   {db_path}")
    print(f"  Sector:      {sector!r}")
    print(f"  Cutoff date: {cutoff_date}")
    print(f"  Output dir:  {out_dir}")
    print()

    conn = sqlite3.connect(str(db_path))
    try:
        known_bio_bodies = conn.execute(
            "SELECT COUNT(*) FROM bodies WHERE raw_json LIKE '%Biological%'"
        ).fetchone()[0]
        candidates = find_stale_bio_candidates(conn, cutoff_date)
    finally:
        conn.close()

    print(f"  {known_bio_bodies} bodies with biological signals in this DB")
    print(f"  {len(candidates)} candidates not reconfirmed since {cutoff_date}")

    slug = sanitize_prefix(sector)
    csv_path = out_dir / f"{slug}_bio_opportunity.csv"
    md_path = out_dir / f"{slug}_bio_opportunity.md"
    write_csv(csv_path, candidates, cutoff_date)
    write_markdown(md_path, candidates, sector, db_path, cutoff_date, known_bio_bodies)
    return md_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find bodies with biological signals not reconfirmed since a cutoff date (default: Odyssey's release)."
    )
    parser.add_argument("--db", type=Path, required=True, help="Path to sector library SQLite file")
    parser.add_argument("--sector", required=True, help="Sector name (for labeling/output filenames)")
    parser.add_argument("--out-dir", type=Path, default=Path("out"), help="Output directory (default: out)")
    parser.add_argument(
        "--cutoff-date", default=ODYSSEY_RELEASE_DATE,
        help=f"YYYY-MM-DD; candidates are signals not reconfirmed since this date (default: {ODYSSEY_RELEASE_DATE}, Odyssey's release)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        return 1

    try:
        run(db_path=args.db, sector=args.sector, out_dir=args.out_dir, cutoff_date=args.cutoff_date)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

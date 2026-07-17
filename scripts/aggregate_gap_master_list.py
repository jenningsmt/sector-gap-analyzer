#!/usr/bin/env python3
"""
aggregate_gap_master_list.py — Merge per-sector gap/extrapolation CSVs into one
master candidate list for exploration flight planning.
=====================================================================================
Scans --out-dir for the CSV outputs of gap_full_export.py and
gap_extrapolate_export.py across any number of sectors, keeps only rows with
edsm_status == "not_in_edsm" (i.e. not reported to Spansh or EDSM — the
candidates actually worth flying to check), tags each with its sector and
candidate type, and writes one combined CSV + Markdown summary.

Deliberately does NOT invent a composite priority/confidence score: the
underlying scripts don't compute one (see docs/gap-finder.md, which describes
a fancier scoring methodology that was never implemented). Ordering is honest
and structural: bracketed gaps first (strongest evidence — missing on both
sides), then backward extrapolation, then forward-chain extrapolation
(sub-sorted by steps_from_edge ascending, since confidence decays the further
a chain has been extended past its last EDSM-confirmed system).

Usage:
  python scripts/aggregate_gap_master_list.py --out-dir out
  python scripts/aggregate_gap_master_list.py --out-dir out --sector outopps --sector oochost
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from scripts import gap_naming
except ImportError:
    import gap_naming

CANDIDATE_TYPE_ORDER = {
    "bracketed_gap": 0, "backward_extrap": 1, "forward_extrap": 2,
    "spatial_gap": 3, "spatial_backward_extrap": 4,
}

# Matches "<slug>_gap_full_validated.csv" / "<slug>_gap_full_dry_run.csv"
RE_GAP_FULL = re.compile(r"^(?P<slug>.+)_gap_full(?:_validated|_dry_run)\.csv$")
# Matches "<slug>_extrap_backward_validated.csv" / "..._dry_run.csv"
RE_BACKWARD = re.compile(r"^(?P<slug>.+)_extrap_backward(?:_validated|_dry_run)\.csv$")
# Matches "<slug>_extrap_forward_step1_validated.csv" and
# "<slug>_extrap_forward_step{N}_chain_validated.csv" (N >= 2) — NOT chain_summary.csv
RE_FORWARD = re.compile(
    r"^(?P<slug>.+)_extrap_forward_step\d+(?:_chain)?(?:_validated|_dry_run)\.csv$"
)
# Matches "<slug>_spatial_gap_validated.csv" / "<slug>_spatial_backward_validated.csv"
# written by gap_spatial_export.py (radius-around-a-system search).
RE_SPATIAL_GAP = re.compile(r"^(?P<slug>.+)_spatial_gap(?:_validated|_dry_run)\.csv$")
RE_SPATIAL_BACKWARD = re.compile(r"^(?P<slug>.+)_spatial_backward(?:_validated|_dry_run)\.csv$")


class Row:
    __slots__ = ("sector_slug", "candidate_type", "system_name", "family",
                 "subsector", "mass_prefix", "mass_code", "boxel", "number",
                 "steps_from_edge", "spansh_edge_number", "source_file")

    def __init__(self, sector_slug, candidate_type, system_name, family,
                 subsector, mass_prefix, mass_code, boxel, number,
                 steps_from_edge, spansh_edge_number, source_file):
        self.sector_slug = sector_slug
        self.candidate_type = candidate_type
        self.system_name = system_name
        self.family = family
        self.subsector = subsector
        self.mass_prefix = mass_prefix
        self.mass_code = mass_code
        self.boxel = boxel
        self.number = number
        self.steps_from_edge = steps_from_edge
        self.spansh_edge_number = spansh_edge_number
        self.source_file = source_file


def classify_file(path: Path) -> Optional[tuple[str, str]]:
    """Return (sector_slug, candidate_type) or None if the file isn't a
    recognized gap/extrapolation CSV (e.g. ring_proximity_*.csv, chain_summary.csv)."""
    name = path.name
    if name.endswith("_extrap_chain_summary.csv") or name.endswith("_extrap_chain_summary_dry_run.csv"):
        return None
    m = RE_GAP_FULL.match(name)
    if m:
        return m.group("slug"), "bracketed_gap"
    m = RE_BACKWARD.match(name)
    if m:
        return m.group("slug"), "backward_extrap"
    m = RE_FORWARD.match(name)
    if m:
        return m.group("slug"), "forward_extrap"
    m = RE_SPATIAL_GAP.match(name)
    if m:
        return m.group("slug"), "spatial_gap"
    m = RE_SPATIAL_BACKWARD.match(name)
    if m:
        return m.group("slug"), "spatial_backward_extrap"
    return None


def load_rows(out_dir: Path, sector_filter: Optional[set[str]]) -> list[Row]:
    rows: list[Row] = []
    for path in sorted(out_dir.glob("*.csv")):
        classified = classify_file(path)
        if classified is None:
            continue
        sector_slug, candidate_type = classified
        if sector_filter and sector_slug not in sector_filter:
            continue

        with path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for rec in reader:
                if rec.get("edsm_status") != "not_in_edsm":
                    continue
                mass_prefix = rec.get("mass_prefix", "")
                # mass_code/boxel columns are only present in CSVs written after
                # the sector/subsector/mass-code/boxel/serial sort rework; derive
                # them from mass_prefix for older files so mixed-vintage out/
                # directories still aggregate correctly.
                if "mass_code" in rec and "boxel" in rec and rec.get("mass_code"):
                    mass_code = rec.get("mass_code", "")
                    try:
                        boxel = int(rec.get("boxel") or 0)
                    except ValueError:
                        boxel = 0
                else:
                    mass_code, boxel = gap_naming.split_mass_prefix(mass_prefix)
                rows.append(Row(
                    sector_slug=sector_slug,
                    candidate_type=candidate_type,
                    system_name=rec.get("system_name", ""),
                    family=rec.get("family", ""),
                    subsector=rec.get("subsector", ""),
                    mass_prefix=mass_prefix,
                    mass_code=mass_code,
                    boxel=boxel,
                    number=rec.get("number", ""),
                    steps_from_edge=rec.get("steps_from_edge", ""),
                    spansh_edge_number=rec.get("spansh_edge_number", ""),
                    source_file=path.name,
                ))
    return rows


def sort_key(row: Row):
    try:
        steps = int(row.steps_from_edge)
    except (TypeError, ValueError):
        steps = 0
    try:
        number = int(row.number)
    except (TypeError, ValueError):
        number = 0
    # In-game order first (sector -> subsector -> mass code -> boxel -> serial)
    # so candidates near each other on the map land together regardless of
    # which methodology (bracketed gap / backward / forward / spatial) found
    # them; candidate type and steps_from_edge are trailing tiebreakers only.
    return (
        row.sector_slug,
        row.subsector,
        row.mass_code,
        row.boxel,
        number,
        CANDIDATE_TYPE_ORDER.get(row.candidate_type, 99),
        steps,
        row.system_name,
    )


def write_master_csv(out_path: Path, rows: list[Row]) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "sector", "candidate_type", "system_name", "family", "subsector",
            "mass_prefix", "mass_code", "boxel", "number", "steps_from_edge",
            "spansh_edge_number", "source_file",
        ])
        for row in rows:
            writer.writerow([
                row.sector_slug, row.candidate_type, row.system_name, row.family,
                row.subsector, row.mass_prefix, row.mass_code, row.boxel,
                row.number, row.steps_from_edge,
                row.spansh_edge_number, row.source_file,
            ])
    print(f"  CSV: {out_path}  ({len(rows)} rows)")


def write_master_md(out_path: Path, rows: list[Row]) -> None:
    by_sector: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_sector[row.sector_slug].append(row)

    counts_by_sector_type: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        counts_by_sector_type[(row.sector_slug, row.candidate_type)] += 1

    with out_path.open("w", encoding="utf-8") as f:
        f.write("# Master Gap / Extrapolation Candidate List\n\n")
        f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Sectors: {len(by_sector)}\n")
        f.write(f"Total candidates (not in EDSM): {len(rows)}\n\n")
        f.write(
            "> Candidates are hypotheses, not confirmed systems. `bracketed_gap` "
            "(missing number strictly between two known systems in a naming "
            "family) is the strongest evidence; `backward_extrap` and "
            "`forward_extrap` are extrapolated beyond the known range and only "
            "as reliable as the `steps_from_edge` value — lower is better.\n\n"
        )
        f.write("## Summary\n\n")
        f.write("| Sector | Bracketed Gap | Backward Extrap | Forward Extrap | Spatial Gap | Spatial Backward | Total |\n")
        f.write("|--------|--------------:|-----------------:|----------------:|------------:|-----------------:|------:|\n")
        for sector_slug in sorted(by_sector):
            bg = counts_by_sector_type.get((sector_slug, "bracketed_gap"), 0)
            be = counts_by_sector_type.get((sector_slug, "backward_extrap"), 0)
            fe = counts_by_sector_type.get((sector_slug, "forward_extrap"), 0)
            sg = counts_by_sector_type.get((sector_slug, "spatial_gap"), 0)
            sb = counts_by_sector_type.get((sector_slug, "spatial_backward_extrap"), 0)
            f.write(f"| {sector_slug} | {bg} | {be} | {fe} | {sg} | {sb} | {bg + be + fe + sg + sb} |\n")
        f.write("\n---\n\n")

        type_labels = {
            "bracketed_gap": "Bracketed Gaps (intra-sequence)",
            "backward_extrap": "Backward Extrapolation (below known min)",
            "forward_extrap": "Forward Extrapolation (beyond known max, chain-confirmed)",
            "spatial_gap": "Spatial Search — Bracketed Gaps",
            "spatial_backward_extrap": "Spatial Search — Backward Extrapolation",
        }

        for sector_slug in sorted(by_sector):
            sector_rows = by_sector[sector_slug]
            f.write(f"## {sector_slug}\n\n")
            by_type: dict[str, list[Row]] = defaultdict(list)
            for row in sector_rows:
                by_type[row.candidate_type].append(row)

            for candidate_type in (
                "bracketed_gap", "backward_extrap", "forward_extrap",
                "spatial_gap", "spatial_backward_extrap",
            ):
                type_rows = by_type.get(candidate_type)
                if not type_rows:
                    continue
                f.write(f"### {type_labels[candidate_type]}\n\n")
                by_family: dict[str, list[Row]] = defaultdict(list)
                for row in type_rows:
                    by_family[row.family or row.subsector or "(unparsed)"].append(row)
                for family in sorted(by_family):
                    family_rows = by_family[family]
                    f.write(f"**{family}** ({len(family_rows)}):\n\n")
                    for row in family_rows:
                        step_note = f"  *(step {row.steps_from_edge})*" if row.steps_from_edge else ""
                        f.write(f"  - {row.system_name}{step_note}\n")
                    f.write("\n")
            f.write("\n")

    print(f"  MD:  {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate per-sector gap/extrapolation CSVs into one master candidate list."
    )
    parser.add_argument("--out-dir", type=Path, default=Path("out"),
                        help="Directory containing gap_full_export.py / gap_extrapolate_export.py CSV outputs (default: out)")
    parser.add_argument("--sector", action="append", default=None,
                        help="Restrict to these sector slugs (repeatable); default: all sectors found")
    parser.add_argument("--output-prefix", default="master_gap_candidates",
                        help="Output filename prefix (default: master_gap_candidates)")
    args = parser.parse_args()

    if not args.out_dir.exists():
        print(f"ERROR: --out-dir not found: {args.out_dir}", file=sys.stderr)
        return 1

    sector_filter = set(args.sector) if args.sector else None

    print(f"Scanning {args.out_dir} for gap/extrapolation CSVs...")
    rows = load_rows(args.out_dir, sector_filter)
    rows.sort(key=sort_key)

    if not rows:
        print("No not_in_edsm candidate rows found. Nothing to aggregate.")
        return 1

    sectors_found = sorted({row.sector_slug for row in rows})
    print(f"  {len(rows)} candidate rows across {len(sectors_found)} sector(s): {', '.join(sectors_found)}")
    print()

    csv_path = args.out_dir / f"{args.output_prefix}.csv"
    md_path = args.out_dir / f"{args.output_prefix}.md"
    write_master_csv(csv_path, rows)
    write_master_md(md_path, rows)

    return 0


if __name__ == "__main__":
    sys.exit(main())

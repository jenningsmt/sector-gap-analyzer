#!/usr/bin/env python3
"""
gap_spatial_export.py — Spatial (radius-around-a-system) gap analysis.
=============================================================================
Companion to gap_full_export.py / gap_extrapolate_export.py. Runs the same
two candidate-generation methods those scripts use -- bracketed gaps and
backward extrapolation -- but scopes the "known systems" universe to a
sphere of --radius-ly around --center-system instead of an entire sector.

This mirrors how edmfi-mfi's planner_strategic "spatial" mode works
(planner_strategic/extract.py::_extract_systems, mode == "spatial"): the
radius-filtered system list becomes the *entire* candidate-generation input,
not just a display filter over full-sector results. A real, undiscovered
system that happens to sit just outside the search radius could in principle
still surface as a false-positive "backward" candidate (its sequence
neighbor inside the radius has no way to know about it) -- this is an
inherent characteristic of scoping the search this way, not a bug, and
EDSM validation still catches any candidate that's actually a known system.

No forward extrapolation here -- edge-relative "how far beyond the known
max" reasoning is normally sector-relative and doesn't translate cleanly to
an arbitrary radius; sector mode (gap_full_export.py / gap_extrapolate_export.py)
is the place for that.

OUTPUTS (written to --out-dir)
-------------------------------
  <slug>_spatial_gap_validated.csv / .md        (bracketed gaps)
  <slug>_spatial_backward_validated.csv / .md   (backward extrapolation)
  <slug> = sanitize_prefix(sector) + "_spatial_" + sanitize_prefix(center_system)

Usage:
  python scripts/gap_spatial_export.py \\
      --db data/sector_library/sector_heart_sector.sqlite \\
      --sector "Heart Sector" --center-system "Heart Sector AA-Q b5-3" \\
      --radius-ly 20 --out-dir out

  # Bracketed gaps only, dry run:
  python scripts/gap_spatial_export.py \\
      --db data/sector_library/sector_heart_sector.sqlite \\
      --sector "Heart Sector" --center-system "Heart Sector AA-Q b5-3" \\
      --radius-ly 20 --direction bracketed_gap --dry-run --out-dir out
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from scripts import gap_naming
    from scripts.gap_full_export import (
        build_sequences,
        generate_bracketed_gaps,
        validate_candidates,
    )
    from scripts.gap_extrapolate_export import (
        ExtrapCandidate,
        build_backward_candidates,
        validate_phase,
    )
    from scripts.extract_sector_systems_to_sqlite import sanitize_prefix
except ImportError:
    import gap_naming
    from gap_full_export import build_sequences, generate_bracketed_gaps, validate_candidates
    from gap_extrapolate_export import ExtrapCandidate, build_backward_candidates, validate_phase
    from extract_sector_systems_to_sqlite import sanitize_prefix

DEFAULT_EXTEND_DEPTH = 5


class CenterSystemNotFoundError(Exception):
    """Raised when --center-system can't be resolved to coordinates in the DB."""


class NoNeighborhoodSystemsError(Exception):
    """Raised when the radius search returns no systems at all."""


# =============================================================================
# Center resolution + radius filter
# =============================================================================

def resolve_center(conn: sqlite3.Connection, center_system: str) -> tuple[str, float, float, float]:
    """Resolve a center system name to (name, x, y, z), tolerating apostrophe
    and casing differences between user input and the stored name."""
    row = conn.execute(
        "SELECT name, x, y, z FROM systems WHERE name = ?", (center_system,)
    ).fetchone()
    if row is None:
        needle = center_system.lower().replace("'", "")
        conn.create_function("_gap_spatial_norm", 1, lambda s: (s or "").lower().replace("'", ""))
        row = conn.execute(
            "SELECT name, x, y, z FROM systems WHERE _gap_spatial_norm(name) = ?", (needle,)
        ).fetchone()
    if row is None:
        raise CenterSystemNotFoundError(
            f"Center system {center_system!r} not found in sector DB. "
            "Check spelling, or extract the sector containing it first."
        )
    name, x, y, z = row
    if x is None or y is None or z is None:
        raise CenterSystemNotFoundError(
            f"Center system {center_system!r} has no coordinates in the sector DB."
        )
    return name, float(x), float(y), float(z)


def load_neighborhood(
    conn: sqlite3.Connection, cx: float, cy: float, cz: float, radius_ly: float
) -> list[str]:
    """All system names within radius_ly (3D Euclidean) of (cx, cy, cz)."""
    radius_sq = radius_ly * radius_ly
    cursor = conn.execute(
        """
        SELECT name FROM systems
        WHERE x IS NOT NULL AND y IS NOT NULL AND z IS NOT NULL
          AND ((x - ?) * (x - ?) + (y - ?) * (y - ?) + (z - ?) * (z - ?)) <= ?
        """,
        (cx, cx, cy, cy, cz, cz, radius_sq),
    )
    return [row[0] for row in cursor.fetchall()]


# =============================================================================
# Output writers
# =============================================================================

def write_spatial_gap_csv(
    out_path: Path,
    candidates: list[str],
    dry_run: bool,
    center_system: str,
    radius_ly: float,
) -> None:
    edsm_status = "skipped" if dry_run else "not_in_edsm"
    rows = []
    for name in candidates:
        parsed = gap_naming.parse_sequence_name(name)
        family, prefix, number = parsed if parsed is not None else ("", "", "")
        rows.append((name, family, prefix, number))

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "system_name", "edsm_status", "direction", "steps_from_edge",
            "spansh_edge_number", "family", "subsector", "mass_prefix",
            "mass_code", "boxel", "number", "center_system", "radius_ly",
        ])
        for name, family, prefix, number in sorted(rows, key=lambda r: gap_naming.group_sort_key(r[0])):
            tokens = [t for t in name.split() if t]
            idx = gap_naming.find_subsector_index(tokens)
            subsector = tokens[idx] if idx is not None else ""
            mass_code, boxel = gap_naming.split_mass_prefix(prefix)
            writer.writerow([
                name, edsm_status, "bracketed_gap", "", "",
                family, subsector, prefix.rstrip("-"), mass_code, boxel, number,
                center_system, radius_ly,
            ])
    print(f"  CSV:  {out_path}  ({len(rows)} rows)")


def write_spatial_gap_markdown(
    out_path: Path,
    candidates: list[str],
    sector: str,
    center_system: str,
    radius_ly: float,
    db_path: Path,
    dry_run: bool,
    neighborhood_count: int,
) -> None:
    ordered = sorted(candidates, key=gap_naming.group_sort_key)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# {sector} — Spatial Search: Bracketed Gaps\n\n")
        f.write(f"Generated      : {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Source         : `{db_path.name}`\n")
        f.write(f"Center system  : `{center_system}`\n")
        f.write(f"Radius         : {radius_ly} ly\n")
        f.write(f"Mode           : {'dry-run (EDSM skipped)' if dry_run else 'EDSM-validated (undiscovered only)'}\n")
        f.write(f"Neighborhood systems: {neighborhood_count}\n")
        f.write(f"Candidates: {len(ordered)}\n\n")
        f.write("---\n\n")

        current_group: Optional[tuple[str, str]] = None
        buf: list[str] = []

        def flush() -> None:
            if not buf or current_group is None:
                return
            subsector, prefix = current_group
            header = " ".join(filter(None, [subsector, prefix]))
            f.write(f"## {sector} {header}\n\n")
            for name in buf:
                f.write(f"  - {name}\n")
            f.write(f"\n*{len(buf)} candidate(s) in this group*\n\n")

        for name in ordered:
            parsed = gap_naming.parse_sequence_name(name)
            tokens = [t for t in name.split() if t]
            idx = gap_naming.find_subsector_index(tokens)
            subsector = tokens[idx] if idx is not None else ""
            prefix = parsed[1].rstrip("-") if parsed is not None else ""
            key = (subsector, prefix)
            if key != current_group:
                flush()
                current_group = key
                buf = []
            buf.append(name)
        flush()

        f.write("---\n\n")
        f.write(f"**Total validated gap candidates: {len(ordered)}**\n")
    print(f"  MD:   {out_path}")


def write_spatial_backward_csv(
    out_path: Path,
    candidates: list[ExtrapCandidate],
    center_system: str,
    radius_ly: float,
) -> None:
    ordered = sorted(
        candidates,
        key=lambda c: (c.edsm_status, c.subsector, c.mass_code, c.boxel, c.number, c.steps_from_edge),
    )
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "system_name", "edsm_status", "direction", "steps_from_edge",
            "spansh_edge_number", "family", "subsector", "mass_prefix",
            "mass_code", "boxel", "number", "center_system", "radius_ly",
        ])
        for c in ordered:
            writer.writerow([
                c.system_name, c.edsm_status, c.direction, c.steps_from_edge,
                c.edge_number, c.family, c.subsector, c.prefix.rstrip("-"),
                c.mass_code, c.boxel, c.number, center_system, radius_ly,
            ])
    print(f"  CSV:  {out_path}  ({len(candidates)} rows)")


def write_spatial_backward_markdown(
    out_path: Path,
    candidates: list[ExtrapCandidate],
    sector: str,
    center_system: str,
    radius_ly: float,
    db_path: Path,
    dry_run: bool,
    neighborhood_count: int,
) -> None:
    in_edsm = [c for c in candidates if c.edsm_status == "in_edsm"]
    not_in_edsm = [c for c in candidates if c.edsm_status == "not_in_edsm"]
    check_failed = [c for c in candidates if c.edsm_status == "check_failed"]

    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# {sector} — Spatial Search: Backward Extrapolation\n\n")
        f.write(f"Generated      : {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Source         : `{db_path.name}`\n")
        f.write(f"Center system  : `{center_system}`\n")
        f.write(f"Radius         : {radius_ly} ly\n")
        f.write(f"Mode           : {'dry-run (EDSM skipped)' if dry_run else 'EDSM-validated'}\n")
        f.write(f"Neighborhood systems: {neighborhood_count}\n")
        f.write(f"Candidates: {len(candidates)}\n")
        if not dry_run:
            f.write(f"  In EDSM (not in Spansh): {len(in_edsm)}\n")
            f.write(f"  Not in EDSM            : {len(not_in_edsm)}\n")
        f.write("\n---\n\n")

        def write_group(title: str, group: list[ExtrapCandidate]) -> None:
            if not group:
                return
            f.write(f"## {title}\n\n")
            ordered = sorted(group, key=lambda c: (c.subsector, c.mass_code, c.boxel, c.number))
            for c in ordered:
                f.write(f"  - {c.system_name}  *(step {c.steps_from_edge} below neighborhood min)*\n")
            f.write(f"\n*{len(group)} candidate(s)*\n\n")

        if dry_run:
            write_group("All Candidates (validation skipped)", candidates)
        else:
            if in_edsm:
                f.write(
                    "> **In EDSM but not in Spansh** — confirmed real systems reported by "
                    "Commanders that have not yet been imported to the Spansh dataset.\n\n"
                )
                write_group("In EDSM — confirmed real, not in Spansh", in_edsm)
            if not_in_edsm:
                f.write(
                    "> **Not in EDSM** — not reported to either Spansh or EDSM. "
                    "May exist undiscovered in-game.\n\n"
                )
                write_group("Not in EDSM — potential undiscovered", not_in_edsm)
            if check_failed:
                write_group("Check failed — not validated either way", check_failed)

        f.write("---\n\n")
        f.write(f"**Phase total: {len(candidates)}**")
        if not dry_run:
            f.write(f"  |  In EDSM: {len(in_edsm)}  |  Not in EDSM: {len(not_in_edsm)}")
        f.write("\n")
    print(f"  MD:   {out_path}")


# =============================================================================
# Main pipeline
# =============================================================================

def run(
    db_path: Path,
    sector: str,
    center_system: str,
    radius_ly: float,
    out_dir: Path,
    dry_run: bool,
    cache_db_path: Optional[Path],
    max_bracket_width: Optional[int] = None,
    extend_depth: int = DEFAULT_EXTEND_DEPTH,
    run_bracketed: bool = True,
    run_backward: bool = True,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    sector = sector.strip()
    center_system = center_system.strip()

    print("Gap Spatial Export")
    print(f"  Source DB     : {db_path}")
    print(f"  Sector        : {sector!r}")
    print(f"  Center system : {center_system!r}")
    print(f"  Radius        : {radius_ly} ly")
    print(f"  Output dir    : {out_dir}")
    print(f"  Dry run       : {dry_run}")
    print()

    conn = sqlite3.connect(str(db_path))
    try:
        resolved_name, cx, cy, cz = resolve_center(conn, center_system)
        if resolved_name != center_system:
            print(f"  Resolved center to: {resolved_name!r}")
        names = load_neighborhood(conn, cx, cy, cz, radius_ly)
    finally:
        conn.close()

    print(f"  {len(names)} systems within {radius_ly} ly of {resolved_name!r}")
    if not names:
        raise NoNeighborhoodSystemsError(
            f"No systems found within {radius_ly} ly of {center_system!r}."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{sanitize_prefix(sector)}_spatial_{sanitize_prefix(resolved_name)}"
    effective_cache = cache_db_path or db_path
    suffix = "_dry_run" if dry_run else "_validated"

    if run_bracketed:
        print(f"\n=== Phase: bracketed gaps ({len(names)} neighborhood systems) ===")
        sequences = build_sequences(names)
        candidates_raw = generate_bracketed_gaps(sequences, max_bracket_width=max_bracket_width)
        known_names = set(names)
        candidates_unique = [c for c in dict.fromkeys(candidates_raw) if c not in known_names]
        print(f"  {len(candidates_unique)} bracketed gap candidates generated")
        validated = validate_candidates(
            candidates_unique, effective_cache, dry_run=dry_run, cancel_event=cancel_event
        )
        stem = f"{slug}_spatial_gap{suffix}"
        write_spatial_gap_csv(out_dir / f"{stem}.csv", validated, dry_run, resolved_name, radius_ly)
        write_spatial_gap_markdown(
            out_dir / f"{stem}.md", validated, sector, resolved_name, radius_ly,
            db_path, dry_run, len(names),
        )

    if cancel_event is not None and cancel_event.is_set():
        print("\nCancelled by user; skipping remaining phases.", flush=True)
        return

    if run_backward:
        print(f"\n=== Phase: backward extrapolation ({len(names)} neighborhood systems) ===")
        bwd_candidates = build_backward_candidates(names, extend_depth)
        print(f"  {len(bwd_candidates)} backward candidates generated")
        validate_phase(bwd_candidates, effective_cache, dry_run, cancel_event=cancel_event)
        stem = f"{slug}_spatial_backward{suffix}"
        write_spatial_backward_csv(out_dir / f"{stem}.csv", bwd_candidates, resolved_name, radius_ly)
        write_spatial_backward_markdown(
            out_dir / f"{stem}.md", bwd_candidates, sector, resolved_name, radius_ly,
            db_path, dry_run, len(names),
        )


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spatial (radius-around-a-system) gap analysis for a sector library database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, required=True, help="Path to sector library SQLite file")
    parser.add_argument("--sector", required=True, help="Sector name the DB belongs to (for labeling/filenames)")
    parser.add_argument("--center-system", required=True, help="System name to search around")
    parser.add_argument("--radius-ly", type=float, required=True, help="Search radius in light years")
    parser.add_argument("--direction", choices=["both", "bracketed_gap", "backward"], default="both",
                        help="Which method(s) to run (default: both)")
    parser.add_argument("--extend-depth", type=int, default=DEFAULT_EXTEND_DEPTH,
                        help=f"Maximum backward extrapolation depth (default: {DEFAULT_EXTEND_DEPTH})")
    parser.add_argument("--max-bracket-width", type=int, default=None,
                        help="Skip bracketed gaps wider than this (default: unlimited)")
    parser.add_argument("--out-dir", type=Path, default=Path("out"), help="Output directory (default: out)")
    parser.add_argument("--cache-db", type=Path, default=None,
                        help="SQLite file for EDSM cache (default: uses --db file)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip EDSM validation; output all generated candidates")

    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    try:
        run(
            db_path=args.db,
            sector=args.sector,
            center_system=args.center_system,
            radius_ly=args.radius_ly,
            out_dir=args.out_dir,
            dry_run=args.dry_run,
            cache_db_path=args.cache_db,
            max_bracket_width=args.max_bracket_width,
            extend_depth=args.extend_depth,
            run_bracketed=args.direction in ("both", "bracketed_gap"),
            run_backward=args.direction in ("both", "backward"),
        )
    except (CenterSystemNotFoundError, NoNeighborhoodSystemsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print("\nDone.")


if __name__ == "__main__":
    main()

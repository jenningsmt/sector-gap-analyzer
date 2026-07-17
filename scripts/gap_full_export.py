#!/usr/bin/env python3
"""
gap_full_export.py — Full gap candidate export for a sector library database.
==============================================================================
Independent script that replicates the gap analysis logic from the edmfi-mfi
planner_strategic pipeline but targets the sector_library SQLite databases
directly and produces a complete list of all EDSM-validated gap candidates.

Key differences from the UI pipeline:
  - Operates directly on a sector_library SQLite (no working DB intermediary)
  - No min_keep early exit — validates all candidates
  - No per-run count cap — generates gaps for every sequence family
  - Output sorted by sector/subsector/mass grouping, then by sequence number
    (structural order for analyst review, not by score)
  - Writes a single Markdown file to data/sector_library/

Usage:
  python scripts/gap_full_export.py \\
      --db data/sector_library/sector_heart_sector.sqlite \\
      --sector "Heart Sector"

  python scripts/gap_full_export.py \\
      --db data/sector_library/sector_soul_sector.sqlite \\
      --sector "Soul Sector"

  # Dry run (generate candidates, skip EDSM validation):
  python scripts/gap_full_export.py \\
      --db data/sector_library/sector_heart_sector.sqlite \\
      --sector "Heart Sector" \\
      --dry-run

Author: CMDR Ariston / Mike (with Claude)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    from scripts import gap_naming
except ImportError:
    import gap_naming

_find_subsector_index = gap_naming.find_subsector_index
_parse_sequence_name = gap_naming.parse_sequence_name


# =============================================================================
# Constants
# =============================================================================

EDSM_BASE_URL = "https://www.edsm.net/api-v1/system"
EDSM_REQUESTS_PER_SEC = 1.0
EDSM_TIMEOUT_SEC = 10.0
EDSM_MAX_RETRIES = 2
EDSM_BACKOFF_BASE = 0.5
EDSM_CACHE_MAX_AGE_DAYS = 7


class NoSequencedSystemsError(Exception):
    """Raised by run() when a sector DB has no matching/sequenced systems.

    Deliberately NOT a sys.exit() -- run() is called directly by the GUI for
    one sector at a time in a multi-sector batch, and one bad sector name
    must not abort the rest of the batch. main() catches this and exits 1.
    """


# =============================================================================
# Name parsing (see gap_naming.py — shared with gap_extrapolate_export.py and
# gap_spatial_export.py)
# =============================================================================
#
# _find_subsector_index / _parse_sequence_name are aliased above from
# gap_naming. Sorting/grouping uses gap_naming.group_sort_key directly (see
# write_full_csv and run()) so the sector/subsector/mass-code/boxel/serial
# grouping order stays identical across all three export scripts.


# =============================================================================
# Gap generation (mirrors planner_strategic/candidates.py)
# =============================================================================

def build_sequences(names: list[str]) -> dict[tuple[str, str], list[int]]:
    """Build a mapping of (family, prefix) → sorted list of known numbers."""
    sequences: dict[tuple[str, str], list[int]] = defaultdict(list)
    for name in names:
        parsed = _parse_sequence_name(name)
        if parsed is None:
            continue
        family, prefix, number = parsed
        sequences[(family, prefix)].append(number)
    return {key: sorted(set(nums)) for key, nums in sequences.items()}


# Backward-compatible alias (kept private for anything still importing the
# old name within this file).
_build_sequences = build_sequences


def generate_bracketed_gaps(
    sequences: dict[tuple[str, str], list[int]],
    max_bracket_width: Optional[int] = None,
) -> list[str]:
    """
    Generate gap candidate names for every sequence family.

    Only nominates numbers that are bracketed by existing systems on both sides —
    i.e. missing values strictly between two *consecutive* known numbers in each
    (family, prefix) sequence. No extrapolation beyond the known range.

    Example: known = [0, 1, 3, 5] → gaps = [2, 4]  (not 6, 7, ...)

    If max_bracket_width is set, a gap between consecutive known numbers L and U
    is only filled when (U - L) <= max_bracket_width; wider gaps are skipped
    entirely (they're far more likely to be numbers Stellar Forge never
    allocated than real missing systems, and filling them without bound can
    produce a huge low-confidence candidate volume — see docs/gap-finder.md
    section 4.3, "dense segment detection").
    """
    candidates: list[str] = []
    for (family, prefix), numbers in sequences.items():
        ordered = sorted(set(numbers))
        for lower, upper in zip(ordered, ordered[1:]):
            gap_width = upper - lower
            if gap_width <= 1:
                continue
            if max_bracket_width is not None and gap_width > max_bracket_width:
                continue
            for n in range(lower + 1, upper):
                candidates.append(f"{family} {prefix}{n}")
    return candidates


_generate_gaps = generate_bracketed_gaps


# =============================================================================
# EDSM client and cache (mirrors planner_strategic/edsm.py)
# =============================================================================

class _EDSMClient:
    def __init__(self) -> None:
        self._last_request_at: Optional[float] = None

    def exists_system(self, system_name: str) -> bool:
        response = self._fetch(system_name)
        if isinstance(response, dict):
            if response.get("name") or response.get("id"):
                return True
        return False

    def _fetch(self, system_name: str) -> dict:
        params = urllib.parse.urlencode({"systemName": system_name, "showId": 1})
        url = f"{EDSM_BASE_URL}?{params}"
        attempt = 0
        while True:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; edmfi-gap-export/1.0)"},
            )
            try:
                self._throttle()
                with urllib.request.urlopen(req, timeout=EDSM_TIMEOUT_SEC) as handle:
                    payload = handle.read().decode("utf-8")
                return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < EDSM_MAX_RETRIES:
                    self._backoff(attempt)
                    attempt += 1
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                if attempt < EDSM_MAX_RETRIES:
                    self._backoff(attempt)
                    attempt += 1
                    continue
                raise

    def _throttle(self) -> None:
        min_interval = 1.0 / EDSM_REQUESTS_PER_SEC
        now = time.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int) -> None:
        time.sleep(EDSM_BACKOFF_BASE * (2 ** attempt))


def _cached_exists(
    conn: sqlite3.Connection, system_name: str, max_age_days: int
) -> Optional[bool]:
    row = conn.execute(
        'SELECT "exists", checked_at FROM edsm_cache WHERE system_name = ?',
        (system_name,),
    ).fetchone()
    if not row:
        return None
    exists_val, checked_at = row
    try:
        checked_time = datetime.fromisoformat(checked_at)
    except ValueError:
        return None
    if datetime.now(timezone.utc) - checked_time > timedelta(days=max_age_days):
        return None
    return bool(exists_val)


def _write_cache(conn: sqlite3.Connection, system_name: str, exists: bool) -> None:
    conn.execute(
        'INSERT OR REPLACE INTO edsm_cache(system_name, "exists", checked_at) VALUES (?, ?, ?)',
        (system_name, int(exists), datetime.now(timezone.utc).isoformat()),
    )


def _ensure_cache_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edsm_cache (
            system_name TEXT PRIMARY KEY,
            "exists" INTEGER NOT NULL,
            checked_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


# =============================================================================
# Validation
# =============================================================================

def validate_candidates(
    candidates: list[str],
    cache_db_path: Path,
    dry_run: bool = False,
    cancel_event: Optional[threading.Event] = None,
) -> list[str]:
    """
    Filter candidates to those NOT found in EDSM (undiscovered systems).

    Uses an EDSM cache stored in cache_db_path to avoid re-checking.
    In dry_run mode, skips EDSM and returns all candidates as-is.
    """
    if dry_run:
        print(f"  [dry-run] Skipping EDSM validation, returning all {len(candidates)} candidates.")
        return candidates

    conn = sqlite3.connect(str(cache_db_path))
    _ensure_cache_table(conn)
    client = _EDSMClient()
    kept: list[str] = []
    total = len(candidates)

    print(f"  Validating {total} candidates via EDSM (1 req/s)...")
    print(f"  Cache: {cache_db_path}")
    print(f"  Estimated max time: ~{total}s (cache hits are instant)")
    print()

    start = time.time()
    cache_hits = 0
    api_calls = 0
    api_errors = 0
    check_failed: list[str] = []

    try:
        for i, name in enumerate(candidates, 1):
            if cancel_event is not None and cancel_event.is_set():
                print(f"  Cancelled by user at {i}/{total}.", flush=True)
                break
            exists = _cached_exists(conn, name, EDSM_CACHE_MAX_AGE_DAYS)
            if exists is not None:
                cache_hits += 1
            else:
                try:
                    exists = client.exists_system(name)
                    api_calls += 1
                except Exception as exc:
                    # A failed check is NOT the same as "confirmed absent from
                    # EDSM" -- exclude it rather than silently keeping it as a
                    # candidate, and don't cache the failure (it may be a
                    # transient/environmental issue, not a real EDSM answer).
                    print(f"  WARNING: EDSM check failed for {name!r}: {exc}", flush=True)
                    api_errors += 1
                    check_failed.append(name)
                    continue
                _write_cache(conn, name, exists)
                conn.commit()

            if not exists:
                kept.append(name)

            if i % 50 == 0 or i == total:
                elapsed = time.time() - start
                rate = i / elapsed if elapsed > 0 else 0
                print(
                    f"  [{i:>5}/{total}] kept={len(kept)}  "
                    f"cache_hits={cache_hits}  api_calls={api_calls}  "
                    f"errors={api_errors}  {elapsed:.0f}s elapsed  ({rate:.1f}/s)",
                    flush=True,
                )

    finally:
        conn.close()

    if check_failed:
        print(
            f"\n  WARNING: {len(check_failed)} candidate(s) could not be checked "
            f"against EDSM (excluded from results, not kept): "
            f"{', '.join(check_failed[:10])}"
            f"{' ...' if len(check_failed) > 10 else ''}"
        )

    elapsed = time.time() - start
    print()
    print(f"  Validation complete: {total} checked, {len(kept)} undiscovered, {elapsed:.0f}s")
    return kept


# =============================================================================
# CSV output (mirrors gap_extrapolate_export.py's per-phase CSV schema so
# results can be merged across sectors/scripts by scripts/aggregate_gap_master_list.py)
# =============================================================================

def write_full_csv(out_path: Path, candidates: list[str], dry_run: bool) -> None:
    edsm_status = "skipped" if dry_run else "not_in_edsm"
    rows = []
    for name in candidates:
        parsed = _parse_sequence_name(name)
        if parsed is None:
            family, prefix, number = "", "", ""
        else:
            family, prefix, number = parsed
        rows.append((name, family, prefix, number))

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "system_name", "edsm_status", "direction", "steps_from_edge",
            "spansh_edge_number", "family", "subsector", "mass_prefix",
            "mass_code", "boxel", "number",
        ])
        for name, family, prefix, number in sorted(rows, key=lambda r: gap_naming.group_sort_key(r[0])):
            tokens = [t for t in name.split() if t]
            idx = _find_subsector_index(tokens)
            subsector = tokens[idx] if idx is not None else ""
            mass_code, boxel = gap_naming.split_mass_prefix(prefix)
            writer.writerow([
                name, edsm_status, "bracketed_gap", "", "",
                family, subsector, prefix.rstrip("-"), mass_code, boxel, number,
            ])
    print(f"  CSV:  {out_path}  ({len(rows)} rows)")


# =============================================================================
# Main pipeline
# =============================================================================

def run(
    db_path: Path,
    sector: str,
    out_dir: Path,
    dry_run: bool,
    cache_db_path: Optional[Path],
    max_bracket_width: Optional[int] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Path:
    sector = sector.strip()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Gap Full Export")
    print(f"  Source DB:  {db_path}")
    print(f"  Sector:     {sector!r}")
    print(f"  Output dir: {out_dir}")
    print(f"  Dry run:    {dry_run}")
    print(f"  Max bracket width: {max_bracket_width if max_bracket_width is not None else 'unlimited'}")
    print()

    # --- Phase 1: Load systems ---
    print("Phase 1: Loading systems from sector DB...")
    like = sector.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + " %"
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM systems WHERE LOWER(name) LIKE LOWER(?) ESCAPE '\\'",
            (like,),
        ).fetchall()
    finally:
        conn.close()

    names = [row[0] for row in rows]
    print(f"  {len(names)} systems loaded matching {like!r}")

    if not names:
        raise NoSequencedSystemsError(
            f"No systems found for sector {sector!r}. Check --sector matches the sector name in the DB."
        )

    # --- Phase 2: Build sequences and generate candidates ---
    print()
    print("Phase 2: Building sequences and generating gap candidates...")
    sequences = _build_sequences(names)
    families_with_sequences = len(sequences)
    print(f"  {families_with_sequences} sequence families found")

    if not sequences:
        raise NoSequencedSystemsError(
            f"No sequenced system names found for sector {sector!r} "
            "(names without trailing -N pattern). Gap analysis requires "
            "systems of the form 'Sector XX-Y zN-M'."
        )

    # Show top families
    top = sorted(sequences.items(), key=lambda kv: -len(kv[1]))[:10]
    print(f"  Top families by depth:")
    for (fam, pfx), nums in top:
        print(f"    {fam} {pfx}  [{min(nums)}-{max(nums)}]  {len(nums)} known")

    candidates_raw = _generate_gaps(sequences, max_bracket_width=max_bracket_width)
    # Deduplicate and remove any that already exist as known systems
    known_names = set(names)
    candidates_unique = [c for c in dict.fromkeys(candidates_raw) if c not in known_names]
    print(f"  {len(candidates_unique)} bracketed gap candidates generated ({len(candidates_raw) - len(candidates_unique)} duplicates/knowns removed)")

    # --- Phase 3: EDSM validation ---
    print()
    print("Phase 3: EDSM validation...")
    effective_cache = cache_db_path or db_path  # default: cache in source DB
    validated = validate_candidates(
        candidates_unique, effective_cache, dry_run=dry_run, cancel_event=cancel_event
    )

    # --- Phase 4: Sort and write output ---
    print()
    print("Phase 4: Sorting and writing output...")

    # Structural sort: (subsector, mass_code, boxel, number) -- in-game order
    validated_sorted = sorted(validated, key=gap_naming.group_sort_key)

    # Build output filename
    sector_slug = re.sub(r"[^a-z0-9]+", "_", sector.lower()).strip("_")
    suffix = "_dry_run" if dry_run else "_validated"
    out_filename = f"{sector_slug}_gap_full{suffix}.md"
    out_path = out_dir / out_filename

    csv_path = out_dir / f"{sector_slug}_gap_full{suffix}.csv"
    write_full_csv(csv_path, validated_sorted, dry_run)

    now = datetime.now(timezone.utc).isoformat()
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# {sector} — Full Gap Candidate List\n\n")
        f.write(f"Generated: {now}\n")
        f.write(f"Source: `{db_path.name}`\n")
        f.write(f"Sector: `{sector}`\n")
        f.write(f"Mode: {'dry-run (EDSM validation skipped)' if dry_run else 'EDSM-validated (undiscovered only)'}\n")
        f.write(f"Candidates: intra-sequence gaps only (bracketed by known systems on both sides)\n")
        f.write(f"Known systems: {len(names)}\n")
        f.write(f"Candidates generated: {len(candidates_unique)}\n")
        f.write(f"Validated (undiscovered): {len(validated_sorted)}\n\n")
        f.write("---\n\n")

        # Group by (subsector, mass_prefix) for section headers
        current_group: Optional[tuple[str, str]] = None
        group_counter = 0
        total_counter = 0

        for name in validated_sorted:
            parsed = _parse_sequence_name(name)
            tokens = [t for t in name.split() if t]
            idx = _find_subsector_index(tokens)
            subsector = tokens[idx] if idx is not None else ""

            if parsed is not None:
                family, prefix, number = parsed
                group_key = (subsector, prefix.rstrip("-"))
            else:
                group_key = (subsector, "")

            if group_key != current_group:
                if current_group is not None:
                    f.write(f"\n*{group_counter} candidate(s) in this group*\n\n")
                current_group = group_key
                group_counter = 0
                # Section header: "subsector mass_code" e.g. "AA-Q b5"
                header = " ".join(filter(None, group_key))
                f.write(f"## {sector} {header}\n\n")

            total_counter += 1
            group_counter += 1
            f.write(f"  {total_counter}. {name}\n")

        # Close last group
        if current_group is not None and group_counter > 0:
            f.write(f"\n*{group_counter} candidate(s) in this group*\n\n")

        f.write("---\n\n")
        f.write(f"**Total validated gap candidates: {len(validated_sorted)}**\n")

    print(f"  Written {len(validated_sorted)} candidates to: {out_path}")
    return out_path


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full gap candidate export for a sector library SQLite database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/gap_full_export.py \\
      --db data/sector_library/sector_heart_sector.sqlite \\
      --sector "Heart Sector"

  # Skip EDSM validation (fast preview):
  python scripts/gap_full_export.py \\
      --db data/sector_library/sector_heart_sector.sqlite \\
      --sector "Heart Sector" \\
      --dry-run
""",
    )
    parser.add_argument(
        "--db", type=Path, required=True,
        help="Path to the sector library SQLite file",
    )
    parser.add_argument(
        "--sector", required=True,
        help="Sector name prefix as it appears in system names (e.g. 'Heart Sector')",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=Path("data/sector_library"),
        help="Output directory for the Markdown report (default: data/sector_library)",
    )
    parser.add_argument(
        "--cache-db", type=Path, default=None,
        help="Path to SQLite file for EDSM cache (default: uses --db file)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip EDSM validation; output all generated candidates",
    )
    parser.add_argument(
        "--max-bracket-width", type=int, default=25,
        help="Skip gaps between consecutive known systems wider than this many "
             "numbers (default: 25; matches docs/gap-finder.md's max_step). "
             "Use 0 or a negative number for unlimited width.",
    )

    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    max_bracket_width = args.max_bracket_width if args.max_bracket_width and args.max_bracket_width > 0 else None

    try:
        out_path = run(
            db_path=args.db,
            sector=args.sector,
            out_dir=args.out_dir,
            max_bracket_width=max_bracket_width,
            dry_run=args.dry_run,
            cache_db_path=args.cache_db,
        )
    except NoSequencedSystemsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print()
    print(f"Done. Report: {out_path}")


if __name__ == "__main__":
    main()

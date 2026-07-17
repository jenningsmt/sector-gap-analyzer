#!/usr/bin/env python3
"""
gap_extrapolate_export.py — Forward/backward sequence extrapolation for sector gap analysis.
============================================================================================
Companion to gap_full_export.py.

FORWARD CHAIN LOGIC
-------------------
Step 1 validates all families (one candidate per family, immediately beyond the Spansh max).
Steps 2+ are chain-extended: a step-N candidate is only generated for a family if the
step-(N-1) candidate was found in EDSM. Once a step returns not_in_edsm, that family's
chain terminates and the last confirmed system is recorded as its EDSM high-water mark.

This keeps steps 2+ vanishingly small regardless of sector size.

BACKWARD LOGIC
--------------
All backward candidates (below the Spansh minimum, down to 0) are run as a single phase.
This is always a small set since sequences rarely start above 0.

OUTPUTS (per phase, written to --out-dir)
-----------------------------------------
  <sector>_extrap_backward_validated.csv / .md
  <sector>_extrap_forward_step1_validated.csv / .md
  <sector>_extrap_forward_step2_chain_validated.csv / .md   (active chains only)
  ...
  <sector>_extrap_chain_summary.csv / .md   (final high-water marks for GalMap review)

Usage:
  # Full run — backward + chained forward:
  python scripts/gap_extrapolate_export.py \\
      --db data/sector_library/sector_pha_aeb.sqlite \\
      --sector "Pha Aeb" --out-dir out

  # Backward only:
  python scripts/gap_extrapolate_export.py \\
      --db data/sector_library/sector_pha_aeb.sqlite \\
      --sector "Pha Aeb" --direction backward --out-dir out

  # Forward step 1 only (~1 hr for a 7k-system sector):
  python scripts/gap_extrapolate_export.py \\
      --db data/sector_library/sector_pha_aeb.sqlite \\
      --sector "Pha Aeb" --direction forward --max-forward-step 1 --out-dir out

  # Dry run — see candidate volumes without hitting EDSM:
  python scripts/gap_extrapolate_export.py \\
      --db data/sector_library/sector_pha_aeb.sqlite \\
      --sector "Pha Aeb" --dry-run --out-dir out
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


# =============================================================================
# Constants
# =============================================================================

EDSM_BASE_URL = "https://www.edsm.net/api-v1/system"
EDSM_REQUESTS_PER_SEC = 1.0
EDSM_TIMEOUT_SEC = 10.0
EDSM_MAX_RETRIES = 2
EDSM_BACKOFF_BASE = 0.5
EDSM_CACHE_MAX_AGE_DAYS = 7

DEFAULT_EXTEND_DEPTH = 5


class NoSequencedSystemsError(Exception):
    """Raised by run() when a sector DB has no matching systems.

    Deliberately NOT a sys.exit() -- run() is called directly by the GUI for
    one sector at a time in a multi-sector batch, and one bad sector name
    must not abort the rest of the batch. main() catches this and exits 1.
    """


# =============================================================================
# Name parsing (see gap_naming.py — shared with gap_full_export.py and
# gap_spatial_export.py)
# =============================================================================

_find_subsector_index = gap_naming.find_subsector_index
_parse_sequence_name = gap_naming.parse_sequence_name
_extract_subsector = gap_naming.extract_subsector


# =============================================================================
# Candidate model
# =============================================================================

class ExtrapCandidate:
    __slots__ = (
        "system_name", "family", "subsector", "prefix", "number",
        "mass_code", "boxel",
        "direction", "steps_from_edge", "edge_number", "edsm_status",
    )

    def __init__(
        self,
        system_name: str,
        family: str,
        subsector: str,
        prefix: str,
        number: int,
        direction: str,
        steps_from_edge: int,
        edge_number: int,   # Spansh known max (forward) or min (backward) — fixed for the chain
    ) -> None:
        self.system_name = system_name
        self.family = family
        self.subsector = subsector
        self.prefix = prefix
        self.number = number
        self.mass_code, self.boxel = gap_naming.split_mass_prefix(prefix)
        self.direction = direction
        self.steps_from_edge = steps_from_edge
        self.edge_number = edge_number
        self.edsm_status: str = "pending"


# =============================================================================
# Candidate generation
# =============================================================================

def build_backward_candidates(names: list[str], extend_depth: int) -> list[ExtrapCandidate]:
    sequences: dict[tuple[str, str], list[int]] = defaultdict(list)
    for name in names:
        parsed = _parse_sequence_name(name)
        if parsed is None:
            continue
        family, prefix, number = parsed
        sequences[(family, prefix)].append(number)

    known_names = set(names)
    candidates: list[ExtrapCandidate] = []

    for (family, prefix), numbers in sorted(sequences.items()):
        min_known = min(numbers)
        subsector = _extract_subsector(family)
        for step in range(1, extend_depth + 1):
            n = min_known - step
            if n < 0:
                break
            name = f"{family} {prefix}{n}"
            if name not in known_names:
                candidates.append(ExtrapCandidate(
                    system_name=name, family=family, subsector=subsector,
                    prefix=prefix, number=n,
                    direction="backward", steps_from_edge=step, edge_number=min_known,
                ))
    return candidates


def build_forward_step1_candidates(names: list[str]) -> list[ExtrapCandidate]:
    """One candidate per sequence family: the number immediately above the Spansh max."""
    sequences: dict[tuple[str, str], list[int]] = defaultdict(list)
    for name in names:
        parsed = _parse_sequence_name(name)
        if parsed is None:
            continue
        family, prefix, number = parsed
        sequences[(family, prefix)].append(number)

    known_names = set(names)
    candidates: list[ExtrapCandidate] = []

    for (family, prefix), numbers in sorted(sequences.items()):
        max_known = max(numbers)
        subsector = _extract_subsector(family)
        n = max_known + 1
        name = f"{family} {prefix}{n}"
        if name not in known_names:
            candidates.append(ExtrapCandidate(
                system_name=name, family=family, subsector=subsector,
                prefix=prefix, number=n,
                direction="forward", steps_from_edge=1, edge_number=max_known,
            ))
    return candidates


def extend_chain_one_step(
    active_chains: dict[tuple[str, str], ExtrapCandidate],
    known_names: set[str],
    step: int,
) -> list[ExtrapCandidate]:
    """
    Generate the next step for each active chain family.
    active_chains maps (family, prefix) -> last confirmed in_edsm candidate.
    """
    candidates: list[ExtrapCandidate] = []
    for (family, prefix), prev in sorted(active_chains.items()):
        n = prev.number + 1
        name = f"{family} {prefix}{n}"
        if name not in known_names:
            candidates.append(ExtrapCandidate(
                system_name=name, family=family, subsector=prev.subsector,
                prefix=prefix, number=n,
                direction="forward", steps_from_edge=step,
                edge_number=prev.edge_number,  # preserve original Spansh edge
            ))
    return candidates


# =============================================================================
# EDSM client and cache
# =============================================================================

class _EDSMClient:
    def __init__(self) -> None:
        self._last_request_at: Optional[float] = None

    def exists_system(self, system_name: str) -> bool:
        response = self._fetch(system_name)
        return bool(isinstance(response, dict) and (response.get("name") or response.get("id")))

    def _fetch(self, system_name: str) -> dict:
        params = urllib.parse.urlencode({"systemName": system_name, "showId": 1})
        url = f"{EDSM_BASE_URL}?{params}"
        attempt = 0
        while True:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; edmfi-gap-extrapolate/1.0)"},
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
        interval = 1.0 / EDSM_REQUESTS_PER_SEC
        now = time.monotonic()
        if self._last_request_at is not None:
            wait = interval - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int) -> None:
        time.sleep(EDSM_BACKOFF_BASE * (2 ** attempt))


def _cached_exists(conn: sqlite3.Connection, system_name: str, max_age_days: int) -> Optional[bool]:
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
            "exists"    INTEGER NOT NULL,
            checked_at  TEXT    NOT NULL
        )
        """
    )
    conn.commit()


# =============================================================================
# Validation
# =============================================================================

def validate_phase(
    candidates: list[ExtrapCandidate],
    cache_db_path: Path,
    dry_run: bool = False,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Resolve edsm_status on each candidate in place."""
    total = len(candidates)

    if dry_run:
        print(f"  [dry-run] Skipping EDSM validation ({total} candidates).")
        for c in candidates:
            c.edsm_status = "skipped"
        return

    conn = sqlite3.connect(str(cache_db_path))
    _ensure_cache_table(conn)

    pre_cached = sum(
        1 for c in candidates
        if _cached_exists(conn, c.system_name, EDSM_CACHE_MAX_AGE_DAYS) is not None
    )
    api_needed = total - pre_cached
    print(f"  {total} candidates  |  {pre_cached} cached  |  {api_needed} API calls  (~{api_needed/60:.0f} min)")
    print()

    client = _EDSMClient()
    start = time.time()
    cache_hits = 0
    api_calls = 0
    api_errors = 0
    in_edsm_count = 0
    check_failed: list[str] = []

    try:
        for i, cand in enumerate(candidates, 1):
            if cancel_event is not None and cancel_event.is_set():
                print(f"  Cancelled by user at {i}/{total}.", flush=True)
                break
            cached = _cached_exists(conn, cand.system_name, EDSM_CACHE_MAX_AGE_DAYS)
            if cached is not None:
                exists = cached
                cache_hits += 1
            else:
                try:
                    exists = client.exists_system(cand.system_name)
                    api_calls += 1
                except Exception as exc:
                    # A failed check is NOT the same as "confirmed absent from
                    # EDSM" -- mark it distinctly (excluded from "not_in_edsm"
                    # results downstream) and don't cache the failure.
                    print(f"  WARNING: EDSM check failed for {cand.system_name!r}: {exc}", flush=True)
                    api_errors += 1
                    check_failed.append(cand.system_name)
                    cand.edsm_status = "check_failed"
                    continue
                _write_cache(conn, cand.system_name, exists)
                conn.commit()

            cand.edsm_status = "in_edsm" if exists else "not_in_edsm"
            if exists:
                in_edsm_count += 1

            if i % 100 == 0 or i == total:
                elapsed = time.time() - start
                remaining = max(0, api_needed - api_calls)
                eta_min = remaining / 60.0
                print(
                    f"  [{i:>6}/{total}]  in_edsm={in_edsm_count:<4} "
                    f"cache={cache_hits}  api={api_calls}  err={api_errors}  "
                    f"elapsed={elapsed:.0f}s  eta~{eta_min:.1f}min",
                    flush=True,
                )
    finally:
        conn.close()

    if check_failed:
        print(
            f"\n  WARNING: {len(check_failed)} candidate(s) could not be checked "
            f"against EDSM (marked check_failed, excluded from not_in_edsm results): "
            f"{', '.join(check_failed[:10])}"
            f"{' ...' if len(check_failed) > 10 else ''}"
        )

    elapsed = time.time() - start
    print(f"\n  Done: {in_edsm_count} in EDSM, {total - in_edsm_count} not in EDSM, {elapsed:.0f}s")


# =============================================================================
# Output writers
# =============================================================================

def write_phase_csv(out_path: Path, candidates: list[ExtrapCandidate]) -> None:
    # edsm_status stays the outermost bucket (it's a different result category,
    # not part of the in-game grouping); within a bucket, order in-game:
    # subsector -> mass code -> boxel -> serial, steps_from_edge as a
    # tiebreaker only.
    ordered = sorted(
        candidates,
        key=lambda c: (c.edsm_status, c.subsector, c.mass_code, c.boxel, c.number, c.steps_from_edge),
    )
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "system_name", "edsm_status", "direction", "steps_from_edge",
            "spansh_edge_number", "family", "subsector", "mass_prefix",
            "mass_code", "boxel", "number",
        ])
        for c in ordered:
            writer.writerow([
                c.system_name, c.edsm_status, c.direction, c.steps_from_edge,
                c.edge_number, c.family, c.subsector, c.prefix.rstrip("-"),
                c.mass_code, c.boxel, c.number,
            ])
    print(f"  CSV:  {out_path}  ({len(candidates)} rows)")


def write_phase_markdown(
    out_path: Path,
    candidates: list[ExtrapCandidate],
    sector: str,
    db_path: Path,
    phase_label: str,
    dry_run: bool,
    known_count: int,
) -> None:
    in_edsm = [c for c in candidates if c.edsm_status == "in_edsm"]
    not_in_edsm = [c for c in candidates if c.edsm_status == "not_in_edsm"]
    check_failed = [c for c in candidates if c.edsm_status == "check_failed"]

    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# {sector} — Extrapolation: {phase_label}\n\n")
        f.write(f"Generated : {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Source    : `{db_path.name}`\n")
        f.write(f"Mode      : {'dry-run (EDSM skipped)' if dry_run else 'EDSM-validated'}\n")
        f.write(f"Known systems in DB: {known_count}\n")
        f.write(f"Candidates this phase: {len(candidates)}\n")
        if not dry_run:
            f.write(f"  In EDSM (not in Spansh): {len(in_edsm)}\n")
            f.write(f"  Not in EDSM            : {len(not_in_edsm)}\n")
        f.write("\n---\n\n")

        if dry_run:
            _write_md_group(f, "All Candidates (validation skipped)", candidates)
        else:
            if in_edsm:
                f.write(
                    "> **In EDSM but not in Spansh** — confirmed real systems reported by "
                    "Commanders that have not yet been imported to the Spansh dataset.\n\n"
                )
                _write_md_group(f, "In EDSM — confirmed real, not in Spansh", in_edsm)
            else:
                f.write("*No candidates found in EDSM for this phase.*\n\n")

            if not_in_edsm:
                f.write(
                    "> **Not in EDSM** — not reported to either Spansh or EDSM. "
                    "May exist undiscovered in-game.\n\n"
                )
                _write_md_group(f, "Not in EDSM — potential undiscovered", not_in_edsm)

            if check_failed:
                f.write(
                    "> **Check failed** — EDSM could not be reached/queried for these "
                    "candidates (network/SSL/API error). NOT included in the "
                    "\"not in EDSM\" results above; re-run to retry.\n\n"
                )
                _write_md_group(f, "Check failed — not validated either way", check_failed)

        f.write(f"\n---\n\n**Phase total: {len(candidates)}**")
        if not dry_run:
            f.write(f"  |  In EDSM: {len(in_edsm)}  |  Not in EDSM: {len(not_in_edsm)}")
            if check_failed:
                f.write(f"  |  Check failed: {len(check_failed)}")
        f.write("\n")

    print(f"  MD:   {out_path}")


def _write_md_group(f, title: str, candidates: list[ExtrapCandidate]) -> None:
    if not candidates:
        return
    f.write(f"## {title}\n\n")
    ordered = sorted(candidates, key=lambda c: (c.subsector, c.mass_code, c.boxel, c.number, c.steps_from_edge))
    current_key: Optional[tuple] = None
    buf: list[ExtrapCandidate] = []

    def flush() -> None:
        if not buf:
            return
        g = buf[0]
        dir_label = "forward" if g.direction == "forward" else "backward"
        edge_label = (
            f"Spansh max = {g.edge_number}" if g.direction == "forward"
            else f"Spansh min = {g.edge_number}"
        )
        f.write(f"### {g.family} {g.prefix.rstrip('-')}  ({dir_label}, {edge_label})\n\n")
        for c in buf:
            f.write(f"  - {c.system_name}  *(step {c.steps_from_edge})*\n")
        f.write(f"\n*{len(buf)} candidate(s)*\n\n")

    for c in ordered:
        key = (c.subsector, c.prefix, c.direction)
        if key != current_key:
            flush()
            current_key = key
            buf = []
        buf.append(c)
    flush()


def write_chain_summary(
    out_dir: Path,
    sector_slug: str,
    sector: str,
    db_path: Path,
    chain_peaks: dict[tuple[str, str], ExtrapCandidate],
    terminated_at: dict[tuple[str, str], int],
    dry_run: bool,
    known_count: int,
) -> None:
    """
    Write the final chain summary: one row per family that had any EDSM hit,
    showing the Spansh known max, the confirmed EDSM max, and the step where
    the chain terminated. This is the GalMap review checklist.
    """
    suffix = "_dry_run" if dry_run else ""
    csv_path = out_dir / f"{sector_slug}_extrap_chain_summary{suffix}.csv"
    md_path = out_dir / f"{sector_slug}_extrap_chain_summary{suffix}.md"

    rows = sorted(
        chain_peaks.items(),
        key=lambda kv: (kv[1].subsector, kv[1].prefix, kv[1].number),
    )

    # CSV
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "family", "subsector", "mass_prefix",
            "spansh_max", "edsm_confirmed_max", "edsm_confirmed_system",
            "steps_extended", "terminated_at_step", "chain_status",
        ])
        for (family, prefix), peak in rows:
            key = (family, prefix)
            term_step = terminated_at.get(key)
            steps_extended = peak.steps_from_edge
            chain_status = "terminated" if term_step else "at_depth_limit"
            writer.writerow([
                family, peak.subsector, prefix.rstrip("-"),
                peak.edge_number, peak.number, peak.system_name,
                steps_extended, term_step or "—", chain_status,
            ])
    print(f"  Chain summary CSV: {csv_path}  ({len(rows)} families)")

    # Markdown
    with md_path.open("w", encoding="utf-8") as f:
        f.write(f"# {sector} — Chain Extension Summary\n\n")
        f.write(f"Generated : {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Source    : `{db_path.name}`\n")
        f.write(f"Known systems in DB: {known_count}\n")
        f.write(f"Families with EDSM extension: {len(rows)}\n\n")
        f.write(
            "> These families extend beyond the Spansh dataset into EDSM. "
            "The **EDSM confirmed max** is the last system in each family confirmed "
            "by EDSM. The next step was not found in EDSM (or depth limit was reached). "
            "Perform a GalMap check starting from the confirmed max to determine whether "
            "additional undiscovered systems exist beyond it.\n\n"
        )
        f.write("---\n\n")

        if not rows:
            f.write("*No families extended beyond the Spansh dataset.*\n")
        else:
            f.write("| Family | Spansh max | EDSM confirmed max | Steps extended | Terminated at | Status |\n")
            f.write("|--------|-----------|-------------------|---------------|--------------|--------|\n")
            for (family, prefix), peak in rows:
                key = (family, prefix)
                term_step = terminated_at.get(key)
                f.write(
                    (
                        f"| {family} {prefix.rstrip('-')} "
                        f"| `{prefix}{peak.edge_number}` "
                        f"| `{peak.system_name}` "
                        f"| {peak.steps_from_edge} "
                        f"| step {term_step} (not in EDSM) "
                    ) if term_step else (
                        f"| {family} {prefix.rstrip('-')} "
                        f"| `{prefix}{peak.edge_number}` "
                        f"| `{peak.system_name}` "
                        f"| {peak.steps_from_edge} "
                        f"| depth limit reached "
                    )
                )
                f.write("| terminated |\n" if term_step else "| at depth limit |\n")

            f.write("\n---\n\n")
            f.write(f"**{len(rows)} familie(s) require GalMap verification.**\n")

    print(f"  Chain summary MD:  {md_path}")


# =============================================================================
# Main pipeline
# =============================================================================

def run(
    db_path: Path,
    sector: str,
    out_dir: Path,
    extend_depth: int,
    direction: str,
    max_forward_step: int,
    dry_run: bool,
    cache_db_path: Optional[Path],
    cancel_event: Optional[threading.Event] = None,
) -> None:
    sector = sector.strip()
    run_backward = direction in ("both", "backward")
    run_forward = direction in ("both", "forward")

    print("Gap Extrapolation Export")
    print(f"  Source DB    : {db_path}")
    print(f"  Sector       : {sector!r}")
    print(f"  Direction    : {direction}")
    if run_backward:
        print(f"  Backward     : up to {extend_depth} step(s) below Spansh min (floor 0)")
    if run_forward:
        print(f"  Forward      : chained, up to {max_forward_step} step(s) beyond Spansh max")
    print(f"  Output dir   : {out_dir}")
    print(f"  Dry run      : {dry_run}")
    print()

    # Load systems
    print("Loading systems from sector DB...")
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
    print(f"  {len(names)} systems loaded")
    if not names:
        raise NoSequencedSystemsError(
            f"No systems found for sector {sector!r}. Check --sector matches the sector name prefix in the DB."
        )

    known_names = set(names)
    out_dir.mkdir(parents=True, exist_ok=True)
    sector_slug = re.sub(r"[^a-z0-9]+", "_", sector.lower()).strip("_")
    effective_cache = cache_db_path or db_path
    suffix = "_dry_run" if dry_run else "_validated"

    # -------------------------------------------------------------------------
    # Backward phase
    # -------------------------------------------------------------------------
    if run_backward:
        bwd_candidates = build_backward_candidates(names, extend_depth)
        print(f"\n=== Phase: backward ({len(bwd_candidates)} candidates) ===")
        validate_phase(bwd_candidates, effective_cache, dry_run, cancel_event=cancel_event)
        stem = f"{sector_slug}_extrap_backward{suffix}"
        write_phase_csv(out_dir / f"{stem}.csv", bwd_candidates)
        write_phase_markdown(
            out_dir / f"{stem}.md", bwd_candidates,
            sector, db_path, "backward", dry_run, len(names),
        )

    if cancel_event is not None and cancel_event.is_set():
        print("\nCancelled by user; skipping remaining phases.", flush=True)
        return

    # -------------------------------------------------------------------------
    # Forward chained phases
    # -------------------------------------------------------------------------
    if run_forward:
        # Step 1: all families
        step1 = build_forward_step1_candidates(names)
        print(f"\n=== Phase: forward step 1 ({len(step1)} candidates) ===")
        validate_phase(step1, effective_cache, dry_run, cancel_event=cancel_event)
        stem1 = f"{sector_slug}_extrap_forward_step1{suffix}"
        write_phase_csv(out_dir / f"{stem1}.csv", step1)
        write_phase_markdown(
            out_dir / f"{stem1}.md", step1,
            sector, db_path, "forward step 1", dry_run, len(names),
        )

        # active_chains: (family, prefix) -> last confirmed in_edsm candidate
        active_chains: dict[tuple[str, str], ExtrapCandidate] = {
            (c.family, c.prefix): c for c in step1 if c.edsm_status == "in_edsm"
        }
        # chain_peaks: the highest confirmed in_edsm candidate per family (updated each step)
        chain_peaks: dict[tuple[str, str], ExtrapCandidate] = dict(active_chains)
        # terminated_at: step number at which each family first returned not_in_edsm
        terminated_at: dict[tuple[str, str], int] = {}

        # Mark families that were active at step 1 but immediately terminated
        for c in step1:
            key = (c.family, c.prefix)
            if c.edsm_status == "not_in_edsm" and key in chain_peaks:
                terminated_at[key] = 1

        # Steps 2+: chain-extend only active families
        for step in range(2, max_forward_step + 1):
            if cancel_event is not None and cancel_event.is_set():
                print("\nCancelled by user; stopping chain extension.", flush=True)
                break

            if not active_chains:
                print(f"\n  No active chains remain after step {step - 1}. Forward complete.")
                break

            step_candidates = extend_chain_one_step(active_chains, known_names, step)
            print(
                f"\n=== Phase: forward step {step} "
                f"({len(step_candidates)} active chain(s) from step {step - 1}) ==="
            )

            if not step_candidates:
                break

            validate_phase(step_candidates, effective_cache, dry_run, cancel_event=cancel_event)
            stem_n = f"{sector_slug}_extrap_forward_step{step}_chain{suffix}"
            write_phase_csv(out_dir / f"{stem_n}.csv", step_candidates)
            write_phase_markdown(
                out_dir / f"{stem_n}.md", step_candidates,
                sector, db_path, f"forward step {step} (chain)", dry_run, len(names),
            )

            # Advance active chains; record terminations
            new_active: dict[tuple[str, str], ExtrapCandidate] = {}
            for c in step_candidates:
                key = (c.family, c.prefix)
                if c.edsm_status == "in_edsm":
                    new_active[key] = c
                    chain_peaks[key] = c
                else:
                    # Chain terminates here; peak is already recorded from previous step
                    terminated_at[key] = step

            active_chains = new_active

        # Any chains still active at max depth are "at depth limit" — record their peak
        # (already in chain_peaks; just don't add a terminated_at entry)

        # Chain summary (only if any family ever had a hit)
        if chain_peaks:
            print(f"\n=== Chain Summary ({len(chain_peaks)} familie(s) with EDSM extension) ===")
            write_chain_summary(
                out_dir, sector_slug, sector, db_path,
                chain_peaks, terminated_at, dry_run, len(names),
            )
        else:
            print("\n  No families extended beyond Spansh. No chain summary written.")


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forward/backward sequence extrapolation for sector gap analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Forward phases are chained: step 2 only runs for families where step 1 hit in EDSM,
step 3 only for families where step 2 hit, and so on. Steps 2+ are typically tiny.
The EDSM cache is shared — restarting skips already-checked names.

Examples:
  python scripts/gap_extrapolate_export.py \\
      --db data/sector_library/sector_pha_aeb.sqlite \\
      --sector "Pha Aeb" --out-dir out

  python scripts/gap_extrapolate_export.py \\
      --db data/sector_library/sector_pha_aeb.sqlite \\
      --sector "Pha Aeb" --direction backward --out-dir out

  python scripts/gap_extrapolate_export.py \\
      --db data/sector_library/sector_pha_aeb.sqlite \\
      --sector "Pha Aeb" --direction forward --max-forward-step 1 --out-dir out

  python scripts/gap_extrapolate_export.py \\
      --db data/sector_library/sector_pha_aeb.sqlite \\
      --sector "Pha Aeb" --dry-run --out-dir out
""",
    )
    parser.add_argument("--db", type=Path, required=True,
                        help="Path to sector library SQLite file")
    parser.add_argument("--sector", required=True,
                        help="Sector name prefix as it appears in system names (e.g. 'Pha Aeb')")
    parser.add_argument("--extend-depth", type=int, default=DEFAULT_EXTEND_DEPTH,
                        help=f"Maximum chain depth (default: {DEFAULT_EXTEND_DEPTH})")
    parser.add_argument("--direction", choices=["both", "forward", "backward"], default="both",
                        help="Which direction(s) to run (default: both)")
    parser.add_argument("--max-forward-step", type=int, default=None,
                        help="Stop forward chain at this step number (default: --extend-depth)")
    parser.add_argument("--out-dir", type=Path, default=Path("out"),
                        help="Output directory (default: out)")
    parser.add_argument("--cache-db", type=Path, default=None,
                        help="SQLite file for EDSM cache (default: uses --db file)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip EDSM validation; output all generated candidates")

    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    max_fwd = args.max_forward_step if args.max_forward_step is not None else args.extend_depth
    if max_fwd > args.extend_depth:
        print(
            f"ERROR: --max-forward-step ({max_fwd}) cannot exceed --extend-depth ({args.extend_depth})",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        run(
            db_path=args.db,
            sector=args.sector,
            out_dir=args.out_dir,
            extend_depth=args.extend_depth,
            direction=args.direction,
            max_forward_step=max_fwd,
            dry_run=args.dry_run,
            cache_db_path=args.cache_db,
        )
    except NoSequencedSystemsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print("\nDone.")


if __name__ == "__main__":
    main()

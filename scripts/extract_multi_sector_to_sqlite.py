"""
Extract multiple sectors' systems (all bodies + rings) from a Spansh galaxy dump
into separate SQLite databases, in a single pass over the source file.

The source dump can be very large (tens to hundreds of GB compressed). Running
extract_sector_systems_to_sqlite.py once per sector means re-reading and
re-parsing the whole file per sector. This script streams it once and routes
each matching system to whichever sector(s) it belongs to.

Example usage:
  python scripts/extract_multi_sector_to_sqlite.py \\
      --sector_prefix "Outopps" --sector_prefix "Oochost" --sector_prefix "Oesotl" \\
      --input G:/source-data-master/galaxy.json.gz
"""

import argparse
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    from scripts.extract_sector_systems_to_sqlite import (
        DEFAULT_COMMIT_EVERY_BODIES,
        DEFAULT_COMMIT_EVERY_RINGS,
        DEFAULT_COMMIT_EVERY_SYSTEMS,
        DEFAULT_INPUT,
        DEFAULT_OUTPUT_DIR,
        DEFAULT_PROGRESS_EVERY_SYSTEMS,
        DEFAULT_PROGRESS_SECONDS,
        detect_format,
        init_db,
        iter_systems_json_doc,
        iter_systems_jsonl,
        matches_prefix,
        process_system,
        sanitize_prefix,
        upsert_bodies,
        upsert_rings,
        upsert_systems,
    )
except ImportError:
    from extract_sector_systems_to_sqlite import (
        DEFAULT_COMMIT_EVERY_BODIES,
        DEFAULT_COMMIT_EVERY_RINGS,
        DEFAULT_COMMIT_EVERY_SYSTEMS,
        DEFAULT_INPUT,
        DEFAULT_OUTPUT_DIR,
        DEFAULT_PROGRESS_EVERY_SYSTEMS,
        DEFAULT_PROGRESS_SECONDS,
        detect_format,
        init_db,
        iter_systems_json_doc,
        iter_systems_jsonl,
        matches_prefix,
        process_system,
        sanitize_prefix,
        upsert_bodies,
        upsert_rings,
        upsert_systems,
    )

class SectorSink:
    """Per-sector SQLite connection + pending row buffers."""

    def __init__(self, prefix: str, db_path: Path) -> None:
        self.prefix = prefix
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        try:
            init_db(self.conn)
        except Exception:
            self.conn.close()
            raise
        self.system_rows: List[Dict] = []
        self.body_rows: List[Dict] = []
        self.ring_rows: List[Dict] = []
        self.systems_written = 0
        self.bodies_written = 0
        self.rings_written = 0
        self.matched_systems = 0

    def add(self, system_row: Dict, body_rows: List[Dict], ring_rows: List[Dict]) -> None:
        self.matched_systems += 1
        self.system_rows.append(system_row)
        self.body_rows.extend(body_rows)
        self.ring_rows.extend(ring_rows)

    def pending(self) -> int:
        return len(self.system_rows) + len(self.body_rows) + len(self.ring_rows)

    def flush(self) -> None:
        if not self.system_rows and not self.body_rows and not self.ring_rows:
            return
        self.conn.execute("BEGIN")
        upsert_systems(self.conn, self.system_rows)
        upsert_bodies(self.conn, self.body_rows)
        upsert_rings(self.conn, self.ring_rows)
        self.conn.commit()
        self.systems_written += len(self.system_rows)
        self.bodies_written += len(self.body_rows)
        self.rings_written += len(self.ring_rows)
        self.system_rows.clear()
        self.body_rows.clear()
        self.ring_rows.clear()

    def close(self) -> None:
        self.flush()
        self.conn.close()


def build_sinks(
    prefixes: List[str],
    output_dir: Path,
) -> List[SectorSink]:
    sinks = []
    for prefix in prefixes:
        db_path = output_dir / f"sector_{sanitize_prefix(prefix)}.sqlite"
        print(f"  {prefix!r} -> {db_path}")
        sinks.append(SectorSink(prefix, db_path))
    return sinks


def run(
    input_path: Path,
    sector_prefixes: List[str],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    case_insensitive: bool = True,
    limit: Optional[int] = None,
    commit_every_systems: int = DEFAULT_COMMIT_EVERY_SYSTEMS,
    commit_every_bodies: int = DEFAULT_COMMIT_EVERY_BODIES,
    commit_every_rings: int = DEFAULT_COMMIT_EVERY_RINGS,
    progress_seconds: int = DEFAULT_PROGRESS_SECONDS,
    progress_every_systems: int = DEFAULT_PROGRESS_EVERY_SYSTEMS,
    cancel_event: Optional[threading.Event] = None,
) -> int:
    """Extract multiple sectors' systems/bodies/rings in a single pass over the dump.

    Return codes: 0 = success, 1 = input/format error, 130 = interrupted
    (KeyboardInterrupt or cancel_event set).
    """
    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        return 1

    parsed_format = detect_format(input_path)
    if parsed_format == "unknown":
        print("Unable to detect galaxy.json format (jsonl vs json document).")
        return 1

    print(f"Extracting {len(sector_prefixes)} sector(s) in a single pass:")
    sinks = build_sinks(sector_prefixes, output_dir)

    systems_scanned = 0
    start_time = time.monotonic()
    last_progress_time = start_time
    last_progress_systems = 0

    def print_progress(force: bool = False) -> None:
        nonlocal last_progress_time, last_progress_systems
        now = time.monotonic()
        elapsed = now - start_time
        if not force:
            if elapsed <= 0:
                return
            if (now - last_progress_time) < progress_seconds and (
                systems_scanned - last_progress_systems
            ) < progress_every_systems:
                return
        last_progress_time = now
        last_progress_systems = systems_scanned
        elapsed_minutes = elapsed / 60.0 if elapsed > 0 else 0.0
        rate = systems_scanned / elapsed if elapsed > 0 else 0.0
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        per_sector = " ".join(f"{s.prefix}={s.matched_systems}" for s in sinks)
        print(
            f"{timestamp} scanned={systems_scanned} elapsed_min={elapsed_minutes:.2f} "
            f"rate={rate:.2f}/s  matched[{per_sector}]",
            flush=True,
        )

    interrupted = False
    try:
        if parsed_format == "jsonl":
            iterator = iter_systems_jsonl(input_path)
        else:
            iterator = iter_systems_json_doc(input_path)

        for system in iterator:
            if cancel_event is not None and cancel_event.is_set():
                interrupted = True
                print("Cancelled by user; flushing pending data...", flush=True)
                break

            systems_scanned += 1
            if limit is not None and systems_scanned > limit:
                break

            if not isinstance(system, dict):
                print_progress()
                continue

            system_name = system.get("name") or system.get("Name") or ""
            if system_name:
                for sink in sinks:
                    if matches_prefix(system_name, sink.prefix, case_insensitive):
                        system_row, body_list, ring_list = process_system(
                            system, sink.prefix, case_insensitive
                        )
                        if system_row is not None:
                            sink.add(system_row, body_list, ring_list)
                            if sink.pending() >= commit_every_systems:
                                sink.flush()
                        break  # a system name can only belong to one sector prefix

            print_progress()

    except KeyboardInterrupt:
        interrupted = True
        print("KeyboardInterrupt received; flushing pending data...", flush=True)
    finally:
        for sink in sinks:
            try:
                sink.close()
            except Exception as exc:
                print(f"  WARNING: error closing sink for {sink.prefix!r}: {exc}")

    elapsed = time.monotonic() - start_time
    elapsed_minutes = elapsed / 60.0 if elapsed > 0 else 0.0
    rate = systems_scanned / elapsed if elapsed > 0 else 0.0

    print()
    print(f"Systems scanned: {systems_scanned}")
    print(f"Elapsed minutes: {elapsed_minutes:.2f}")
    print(f"Average rate: {rate:.2f} systems/sec")
    print()
    for sink in sinks:
        print(
            f"  {sink.prefix!r}: {sink.systems_written} systems, "
            f"{sink.bodies_written} bodies, {sink.rings_written} rings -> {sink.db_path}"
        )

    if interrupted:
        return 130
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract multiple sectors from a Spansh galaxy dump into separate SQLite DBs in one pass."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Path to galaxy.json / galaxy.json.gz")
    parser.add_argument(
        "--sector_prefix",
        action="append",
        required=True,
        help="Sector name prefix to extract; repeat for multiple sectors",
    )
    parser.add_argument("--output_dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--case_insensitive", action="store_true", default=True)
    parser.add_argument("--case_sensitive", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Stop after scanning this many systems (sampling)")
    parser.add_argument("--commit-every-systems", type=int, default=DEFAULT_COMMIT_EVERY_SYSTEMS)
    parser.add_argument("--commit-every-bodies", type=int, default=DEFAULT_COMMIT_EVERY_BODIES)
    parser.add_argument("--commit-every-rings", type=int, default=DEFAULT_COMMIT_EVERY_RINGS)
    parser.add_argument("--progress-seconds", type=int, default=DEFAULT_PROGRESS_SECONDS)
    parser.add_argument("--progress-every-systems", type=int, default=DEFAULT_PROGRESS_EVERY_SYSTEMS)
    args = parser.parse_args()

    return run(
        input_path=Path(args.input),
        sector_prefixes=args.sector_prefix,
        output_dir=Path(args.output_dir),
        case_insensitive=not args.case_sensitive,
        limit=args.limit,
        commit_every_systems=args.commit_every_systems,
        commit_every_bodies=args.commit_every_bodies,
        commit_every_rings=args.commit_every_rings,
        progress_seconds=args.progress_seconds,
        progress_every_systems=args.progress_every_systems,
    )


if __name__ == "__main__":
    sys.exit(main())

"""
Extract sector systems (all bodies + rings) from a Spansh galaxy dump into SQLite.

Example usage:
  python tools/extract_sector_systems_to_sqlite.py --sector_prefix "Egnairs "
  python tools/extract_sector_systems_to_sqlite.py
"""

import argparse
import gzip
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from app.config import get_sector_library_dir, resolve_sector_db_path
except Exception:
    get_sector_library_dir = None
    resolve_sector_db_path = None


def _default_input_path() -> Path:
    env_path = os.getenv("MFI_GALAXY_DUMP")
    if env_path:
        return Path(env_path)
    repo_root = Path(__file__).resolve().parents[1]
    gz_path = repo_root / "data" / "source_data" / "galaxy.json.gz"
    if gz_path.exists():
        return gz_path
    return repo_root / "data" / "source_data" / "galaxy.json"


DEFAULT_INPUT = _default_input_path()
_default_library = None
if get_sector_library_dir is not None:
    _default_library = get_sector_library_dir()
else:
    _override = os.getenv("MFI_SECTOR_LIBRARY_DIR")
    if _override:
        _default_library = Path(_override)
    else:
        _default_library = Path(__file__).resolve().parents[1] / "data" / "sector_library"
DEFAULT_OUTPUT_DIR = _default_library
DEFAULT_COMMIT_EVERY_SYSTEMS = 20000
DEFAULT_COMMIT_EVERY_BODIES = 100000
DEFAULT_COMMIT_EVERY_RINGS = 100000
DEFAULT_PROGRESS_SECONDS = 30
DEFAULT_PROGRESS_EVERY_SYSTEMS = 50000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def open_text(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def detect_format(path: Path) -> str:
    with open_text(path) as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("["):
                return "json_doc"
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    return "unknown"
                if isinstance(obj, dict):
                    return "jsonl"
            if stripped.startswith("{") and not stripped.endswith("}"):
                return "json_doc"
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                return "unknown"
            if isinstance(obj, dict):
                return "jsonl"
            return "json_doc"
    return "unknown"


def safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_sql_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value


def first_value(data: Dict[str, Any], keys: Iterable[str]) -> Optional[Any]:
    for key in keys:
        if key in data:
            value = data.get(key)
            if value is not None:
                return value
    return None


def string_value(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    return None


def get_system_name(system: Dict[str, Any]) -> Optional[str]:
    return string_value(first_value(system, ["name", "systemName"]))


def get_system_address(system: Dict[str, Any]) -> Optional[int]:
    value = first_value(system, ["SystemAddress", "systemAddress", "id64"])
    if isinstance(value, int):
        return value
    return safe_int(value)


def get_system_coords(system: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    coords = system.get("coords")
    if isinstance(coords, dict):
        return (
            safe_float(coords.get("x")),
            safe_float(coords.get("y")),
            safe_float(coords.get("z")),
        )
    return (
        safe_float(system.get("x")),
        safe_float(system.get("y")),
        safe_float(system.get("z")),
    )


def extract_bodies(system: Dict[str, Any]) -> List[Dict[str, Any]]:
    bodies = first_value(system, ["bodies", "Bodies"])
    if isinstance(bodies, list):
        return [body for body in bodies if isinstance(body, dict)]
    return []


def extract_rings(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    rings = first_value(body, ["rings", "Rings"])
    if isinstance(rings, list):
        return [ring for ring in rings if isinstance(ring, dict)]
    return []


def normalize_ring_class(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value.strip()


def system_key_for(system_address: Optional[int], system_name: str) -> str:
    if system_address is not None:
        return f"addr:{system_address}"
    return f"name:{system_name}"


def body_key_for(body_id: Optional[int], body_name: str) -> str:
    if body_id is not None:
        return f"id:{body_id}"
    return f"name:{body_name}"


def synth_ring_name(
    body_name: str,
    ring_class: Optional[str],
    inner_rad: Optional[float],
    outer_rad: Optional[float],
) -> str:
    ring_class_str = ring_class or "Unknown"
    inner_str = f"{inner_rad:.0f}" if inner_rad is not None else "?"
    outer_str = f"{outer_rad:.0f}" if outer_rad is not None else "?"
    return f"{body_name} {ring_class_str} {inner_str}-{outer_str}"


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS systems (
            system_key TEXT PRIMARY KEY,
            system_address INTEGER NULL,
            name TEXT UNIQUE,
            x REAL NULL,
            y REAL NULL,
            z REAL NULL,
            raw_json TEXT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bodies (
            system_key TEXT NOT NULL,
            system_address INTEGER NULL,
            system_name TEXT NOT NULL,
            body_key TEXT NOT NULL,
            body_id INTEGER NULL,
            body_name TEXT NOT NULL,
            body_type TEXT NULL,
            sub_type TEXT NULL,
            terraform_state TEXT NULL,
            raw_json TEXT NULL,
            PRIMARY KEY (system_key, body_key)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rings (
            system_key TEXT NOT NULL,
            system_address INTEGER NULL,
            system_name TEXT NOT NULL,
            body_key TEXT NOT NULL,
            body_id INTEGER NULL,
            body_name TEXT NOT NULL,
            ring_name TEXT NOT NULL,
            ring_class TEXT NULL,
            mass_mt REAL NULL,
            inner_rad REAL NULL,
            outer_rad REAL NULL,
            reserve_level TEXT NULL,
            raw_json TEXT NULL,
            PRIMARY KEY (system_key, ring_name)
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_systems_name ON systems(name);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bodies_system ON bodies(system_key);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rings_system ON rings(system_key);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rings_body ON rings(body_key);")
    conn.commit()


def iter_systems_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with open_text(path) as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def iter_systems_json_doc(path: Path) -> Iterable[Dict[str, Any]]:
    print("Detected JSON array document; streaming via ijson.")
    try:
        import ijson
    except ImportError as exc:
        raise RuntimeError("Install ijson for JSON array streaming: pip install ijson") from exc
    with gzip.open(path, "rb") as handle:
        for obj in ijson.items(handle, "item"):
            if isinstance(obj, dict):
                yield obj


def sanitize_prefix(prefix: str) -> str:
    trimmed = prefix.strip()
    replaced = trimmed.replace(" ", "_")
    safe = "".join(ch for ch in replaced if ch.isalnum() or ch in {"_", "-"})
    return safe or "sector"


def prompt_sector_prefix() -> str:
    print("Case-insensitive matching is ON by default in interactive mode.")
    while True:
        sys.stdout.write(
            "Enter sector/system-name prefix to extract (example: Egnairs or Eotchorts): "
        )
        sys.stdout.flush()
        value = sys.stdin.readline()
        if value is None:
            value = ""
        value = value.rstrip("\r\n")
        if value:
            return value
        print("Prefix cannot be empty.")


def process_system(
    system: Dict[str, Any],
    sector_prefix: str,
    case_insensitive: bool,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    system_name = system.get("name") or system.get("Name") or ""
    if not system_name:
        return None, [], []

    if case_insensitive:
        if not system_name.lower().startswith(sector_prefix.lower()):
            return None, [], []
    else:
        if not system_name.startswith(sector_prefix):
            return None, [], []

    system_address = get_system_address(system)
    system_key = system_key_for(system_address, system_name)
    x, y, z = get_system_coords(system)

    system_row = {
        "system_key": system_key,
        "system_address": system_address,
        "name": system_name,
        "x": x,
        "y": y,
        "z": z,
        "raw_json": json.dumps(system, ensure_ascii=True, default=str),
    }

    body_rows: List[Dict[str, Any]] = []
    ring_rows: List[Dict[str, Any]] = []
    bodies = extract_bodies(system)
    for body in bodies:
        body_name = string_value(body.get("name"))
        if not body_name:
            continue
        body_id = safe_int(first_value(body, ["bodyId", "BodyID", "id"]))
        body_key = body_key_for(body_id, body_name)
        body_type = string_value(first_value(body, ["type", "bodyType", "class", "planetClass"]))
        sub_type = string_value(first_value(body, ["subType", "subtype", "subClass", "subclass"]))
        terraform_state = string_value(first_value(body, ["terraformingState", "terraformState"]))

        body_rows.append(
            {
                "system_key": system_key,
                "system_address": system_address,
                "system_name": system_name,
                "body_key": body_key,
                "body_id": body_id,
                "body_name": body_name,
                "body_type": body_type,
                "sub_type": sub_type,
                "terraform_state": terraform_state,
                "raw_json": json.dumps(body, ensure_ascii=True, default=str),
            }
        )

        for ring in extract_rings(body):
            ring_name = string_value(first_value(ring, ["name", "Name", "ringName", "RingName"]))
            ring_class = normalize_ring_class(first_value(ring, ["ringClass", "RingClass", "class"]))
            mass_mt = safe_float(first_value(ring, ["massMT", "MassMT"]))
            inner_rad = safe_float(first_value(ring, ["innerRad", "InnerRad"]))
            outer_rad = safe_float(first_value(ring, ["outerRad", "OuterRad"]))
            reserve_level = string_value(first_value(ring, ["reserveLevel", "ReserveLevel"]))
            if not ring_name:
                ring_name = synth_ring_name(body_name, ring_class, inner_rad, outer_rad)

            ring_rows.append(
                {
                    "system_key": system_key,
                    "system_address": system_address,
                    "system_name": system_name,
                    "body_key": body_key,
                    "body_id": body_id,
                    "body_name": body_name,
                    "ring_name": ring_name,
                    "ring_class": ring_class,
                    "mass_mt": mass_mt,
                    "inner_rad": inner_rad,
                    "outer_rad": outer_rad,
                    "reserve_level": reserve_level,
                    "raw_json": json.dumps(ring, ensure_ascii=True, default=str),
                }
            )

    return system_row, body_rows, ring_rows


def upsert_systems(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO systems (
            system_key,
            system_address,
            name,
            x,
            y,
            z,
            raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["system_key"],
                row["system_address"],
                row["name"],
                row["x"],
                row["y"],
                row["z"],
                row["raw_json"],
            )
            for row in rows
        ],
    )


def upsert_bodies(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO bodies (
            system_key,
            system_address,
            system_name,
            body_key,
            body_id,
            body_name,
            body_type,
            sub_type,
            terraform_state,
            raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["system_key"],
                row["system_address"],
                row["system_name"],
                row["body_key"],
                row["body_id"],
                row["body_name"],
                row["body_type"],
                row["sub_type"],
                row["terraform_state"],
                row["raw_json"],
            )
            for row in rows
        ],
    )


def upsert_rings(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO rings (
            system_key,
            system_address,
            system_name,
            body_key,
            body_id,
            body_name,
            ring_name,
            ring_class,
            mass_mt,
            inner_rad,
            outer_rad,
            reserve_level,
            raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["system_key"],
                row["system_address"],
                row["system_name"],
                row["body_key"],
                row["body_id"],
                row["body_name"],
                row["ring_name"],
                row["ring_class"],
                row["mass_mt"],
                row["inner_rad"],
                row["outer_rad"],
                row["reserve_level"],
                row["raw_json"],
            )
            for row in rows
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract sector systems from a Spansh galaxy dump into SQLite."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output_db", default=None)
    parser.add_argument("--sector_prefix", default=None)
    parser.add_argument("--case_insensitive", action="store_true")
    parser.add_argument("--case_sensitive", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--debug_prefix", action="store_true")
    parser.add_argument("--stop_after_matches", type=int, default=0)
    parser.add_argument("--commit-every-systems", type=int, default=DEFAULT_COMMIT_EVERY_SYSTEMS)
    parser.add_argument("--commit-every-bodies", type=int, default=DEFAULT_COMMIT_EVERY_BODIES)
    parser.add_argument("--commit-every-rings", type=int, default=DEFAULT_COMMIT_EVERY_RINGS)
    parser.add_argument("--progress-seconds", type=int, default=DEFAULT_PROGRESS_SECONDS)
    parser.add_argument("--progress-every-systems", type=int, default=DEFAULT_PROGRESS_EVERY_SYSTEMS)
    args = parser.parse_args()

    if args.case_sensitive and args.case_insensitive:
        print("Choose only one of --case_insensitive or --case_sensitive.")
        return 1

    sector_prefix = args.sector_prefix
    interactive_prompt = False
    if not sector_prefix:
        interactive_prompt = True
        sector_prefix = prompt_sector_prefix()

    sector_prefix = sector_prefix.rstrip("\r\n")
    if interactive_prompt:
        case_insensitive = not args.case_sensitive
    else:
        case_insensitive = args.case_insensitive and not args.case_sensitive

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        return 1

    output_db = args.output_db
    if not output_db:
        if resolve_sector_db_path is not None:
            output_db = resolve_sector_db_path(sector_prefix)
        else:
            sanitized = sanitize_prefix(sector_prefix)
            output_db = str(DEFAULT_OUTPUT_DIR / f"sector_{sanitized}.sqlite")

    db_path = Path(output_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    parsed_format = detect_format(input_path)
    if parsed_format == "unknown":
        print("Unable to detect galaxy.json format (jsonl vs json document).")
        return 1

    conn = sqlite3.connect(db_path)
    init_db(conn)

    systems_scanned = 0
    systems_written = 0
    bodies_written = 0
    rings_written = 0
    systems_with_rings = 0
    matched_systems = 0

    system_rows: List[Dict[str, Any]] = []
    body_rows: List[Dict[str, Any]] = []
    ring_rows: List[Dict[str, Any]] = []

    start_time = time.monotonic()
    last_progress_time = start_time
    last_progress_systems = 0

    def flush() -> None:
        nonlocal systems_written, bodies_written, rings_written
        if not system_rows and not body_rows and not ring_rows:
            return
        conn.execute("BEGIN")
        upsert_systems(conn, system_rows)
        upsert_bodies(conn, body_rows)
        upsert_rings(conn, ring_rows)
        conn.commit()
        systems_written += len(system_rows)
        bodies_written += len(body_rows)
        rings_written += len(ring_rows)
        system_rows.clear()
        body_rows.clear()
        ring_rows.clear()

    def print_progress(force: bool = False) -> None:
        nonlocal last_progress_time, last_progress_systems
        now = time.monotonic()
        elapsed = now - start_time
        if not force:
            if elapsed <= 0:
                return
            if (now - last_progress_time) < args.progress_seconds and (
                systems_scanned - last_progress_systems
            ) < args.progress_every_systems:
                return
        last_progress_time = now
        last_progress_systems = systems_scanned
        elapsed_minutes = elapsed / 60.0 if elapsed > 0 else 0.0
        rate = systems_scanned / elapsed if elapsed > 0 else 0.0
        pending_systems = len(system_rows)
        pending_bodies = len(body_rows)
        pending_rings = len(ring_rows)
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        print(
            (
                f"{timestamp} scanned={systems_scanned} systems_written={systems_written} "
                f"bodies_written={bodies_written} rings_written={rings_written} "
                f"pending_systems={pending_systems} pending_bodies={pending_bodies} "
                f"pending_rings={pending_rings} elapsed_min={elapsed_minutes:.2f} "
                f"rate={rate:.2f}/s"
            ),
            flush=True,
        )

    debug_names_seen = 0
    debug_match_seen = 0
    interrupted = False
    try:
        if parsed_format == "jsonl":
            iterator = iter_systems_jsonl(input_path)
        else:
            iterator = iter_systems_json_doc(input_path)

        for system in iterator:
            systems_scanned += 1
            if args.limit is not None and systems_scanned >= args.limit:
                break

            if not isinstance(system, dict):
                print_progress()
                continue

            if args.debug_prefix and debug_names_seen < 5:
                name_preview = system.get("name") or system.get("Name") or ""
                print(f"debug system_name[{debug_names_seen}]: {name_preview!r}")
                debug_names_seen += 1
                if debug_names_seen == 1:
                    print(f"debug sector_prefix: {sector_prefix!r}")
                    print(f"debug case_insensitive: {case_insensitive}")

            system_row, body_list, ring_list = process_system(
                system, sector_prefix, case_insensitive
            )
            if args.debug_prefix and debug_match_seen < 5:
                name_preview = system.get("name") or system.get("Name") or ""
                matched = system_row is not None
                print(f"debug match[{debug_match_seen}]: {name_preview!r} -> {matched}")
                debug_match_seen += 1
            if system_row is None:
                print_progress()
                continue

            matched_systems += 1
            systems_with_rings += 1 if ring_list else 0
            system_rows.append(system_row)
            body_rows.extend(body_list)
            ring_rows.extend(ring_list)

            if (
                len(system_rows) >= args.commit_every_systems
                or len(body_rows) >= args.commit_every_bodies
                or len(ring_rows) >= args.commit_every_rings
            ):
                flush()

            if args.verbose:
                print_progress()

            if args.debug_prefix and systems_scanned % 1_000_000 == 0:
                name_preview = system.get("name") or system.get("Name") or ""
                print(
                    f"debug sample@{systems_scanned}: {name_preview!r} match_count={matched_systems}"
                )

            if args.stop_after_matches and matched_systems >= args.stop_after_matches:
                break

        flush()
    except KeyboardInterrupt:
        interrupted = True
        print("KeyboardInterrupt received; flushing pending data...", flush=True)
        try:
            flush()
        except Exception:
            conn.rollback()
    finally:
        conn.close()

    elapsed = time.monotonic() - start_time
    elapsed_minutes = elapsed / 60.0 if elapsed > 0 else 0.0
    rate = systems_scanned / elapsed if elapsed > 0 else 0.0

    print(f"Systems scanned: {systems_scanned}")
    print(f"Systems inserted: {systems_written}")
    print(f"Bodies inserted: {bodies_written}")
    print(f"Rings inserted: {rings_written}")
    print(f"Systems with >=1 ring: {systems_with_rings}")
    print(f"Elapsed minutes: {elapsed_minutes:.2f}")
    print(f"Average rate: {rate:.2f} systems/sec")
    print(f"Database written to: {db_path}")
    if interrupted:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

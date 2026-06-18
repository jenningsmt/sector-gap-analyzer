"""Minimal extraction from a sector sqlite into the working sqlite."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from app.config import detect_sector_from_system, get_sector_db_override, resolve_sector_db_path
from planner_strategic import db
from planner_strategic.models import JobParams, JobProgress


def extract_minimal(
    working_db_path: str,
    params: JobParams,
    progress_cb: Optional[callable] = None,
) -> None:
    _emit(progress_cb, JobProgress(phase="extract", current=0, total=0, message="Initializing"))
    db.ensure_working_db(working_db_path)

    sector_db_path = _resolve_sector_db_path(params)
    _emit(
        progress_cb,
        JobProgress(phase="extract", current=0, total=0, message=f"Source DB: sector {sector_db_path}"),
    )
    try:
        source = sqlite3.connect(sector_db_path)
    except sqlite3.Error as exc:
        raise ValueError(f"Unable to open sector DB: {sector_db_path}") from exc

    available = _count_systems(source)
    _emit(
        progress_cb,
        JobProgress(
            phase="extract",
            current=available,
            total=available,
            message=f"Sector systems available: {available}",
        ),
    )

    dest = sqlite3.connect(working_db_path)
    try:
        dest.execute("DELETE FROM working_systems")
        if not _table_exists(source, "strategic_seed"):
            _emit(progress_cb, JobProgress(phase="extract", current=0, total=0, message="No seed table"))
        else:
            dest.execute("DELETE FROM strategic_seed")
            rows = _fetch_seed_rows(source, params)
            dest.executemany(
                """
                INSERT INTO strategic_seed(
                    mode, sector, center_system, radius_ly, system_name,
                    base_score, distance_ly, tags, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            dest.commit()
            _emit(
                progress_cb,
                JobProgress(phase="extract", current=len(rows), total=len(rows), message="Seed copied"),
            )

        inserted = _extract_systems(source, dest, params, progress_cb)
        _emit(
            progress_cb,
            JobProgress(
                phase="extract",
                current=inserted,
                total=inserted,
                message=f"working_systems inserted: {inserted}",
            ),
        )
    finally:
        dest.close()
        source.close()


def _fetch_seed_rows(conn: sqlite3.Connection, params: JobParams) -> list[tuple]:
    if params.mode == "gap":
        return list(
            conn.execute(
                """
                SELECT mode, sector, center_system, radius_ly, system_name,
                       base_score, distance_ly, tags, rationale
                FROM strategic_seed
                WHERE mode = 'gap' AND (? IS NULL OR sector = ?)
                """,
                (params.sector, params.sector),
            )
        )
    elif params.mode == "spatial":
        return list(
            conn.execute(
                """
                SELECT mode, sector, center_system, radius_ly, system_name,
                       base_score, distance_ly, tags, rationale
                FROM strategic_seed
                WHERE mode = 'spatial' AND (? IS NULL OR center_system = ?)
                """,
                (params.center_system, params.center_system),
            )
        )
    else:  # hot_subsector
        return list(
            conn.execute(
                """
                SELECT mode, sector, center_system, radius_ly, system_name,
                       base_score, distance_ly, tags, rationale
                FROM strategic_seed
                WHERE mode = 'hot_subsector' AND (? IS NULL OR sector = ?)
                """,
                (params.sector, params.sector),
            )
        )


def _extract_systems(
    source: sqlite3.Connection,
    dest: sqlite3.Connection,
    params: JobParams,
    progress_cb: Optional[callable],
) -> int:
    if not _table_exists(source, "systems"):
        raise ValueError("Systems table not found in sector DB")
    table_name = "systems"
    name_col = "name"
    x_col = "x"
    y_col = "y"
    z_col = "z"

    dest.execute("DELETE FROM working_systems")
    inserted = 0

    if params.mode == "gap":
        if not params.sector:
            return 0
        prefix = params.sector.strip()
        if not prefix:
            return 0
        like = prefix + " %"
        cursor = source.execute(
            f"""
            SELECT {name_col}, {x_col}, {y_col}, {z_col}
            FROM {table_name}
            WHERE LOWER({name_col}) LIKE LOWER(?)
            """,
            (like,),
        )
        rows = cursor.fetchall()
        dest.executemany(
            "INSERT OR REPLACE INTO working_systems(system_name, x, y, z) VALUES (?, ?, ?, ?)",
            rows,
        )
        inserted = len(rows)

    elif params.mode == "spatial":
        if not params.center_system:
            return 0
        # Exact match first; fall back to normalised match to tolerate user-input
        # variations: apostrophe differences ("Parrot's" vs "Parrots") and
        # capitalisation differences ("EL-Y D70" vs "EL-Y d70").
        center = source.execute(
            f"SELECT {name_col}, {x_col}, {y_col}, {z_col} FROM {table_name} WHERE {name_col} = ?",
            (params.center_system,),
        ).fetchone()
        if not center:
            needle = params.center_system.lower().replace("'", "")
            source.create_function("_mfi_norm", 1, lambda s: (s or "").lower().replace("'", ""))
            center = source.execute(
                f"SELECT {name_col}, {x_col}, {y_col}, {z_col} FROM {table_name} WHERE _mfi_norm({name_col}) = ?",
                (needle,),
            ).fetchone()
        if not center:
            _emit(progress_cb, JobProgress(phase="extract", current=0, total=0, message="Center not found"))
            raise ValueError("Center system not found in sector DB")
        _, cx, cy, cz = center
        radius = float(params.radius_ly or 0.0)
        if radius <= 0:
            return 0
        radius_sq = radius * radius
        cursor = source.execute(
            f"""
            SELECT {name_col}, {x_col}, {y_col}, {z_col}
            FROM {table_name}
            WHERE (({x_col} - ?)*({x_col} - ?) + ({y_col} - ?)*({y_col} - ?) + ({z_col} - ?)*({z_col} - ?)) <= ?
            """,
            (cx, cx, cy, cy, cz, cz, radius_sq),
        )
        rows = cursor.fetchall()
        dest.executemany(
            "INSERT OR REPLACE INTO working_systems(system_name, x, y, z) VALUES (?, ?, ?, ?)",
            rows,
        )
        inserted = len(rows)
        _emit(
            progress_cb,
            JobProgress(
                phase="extract",
                current=inserted,
                total=inserted,
                message="Neighborhood systems count",
            ),
        )

    elif params.mode == "hot_subsector":
        if not params.sector or not params.sub_sector or not params.mass_code:
            return 0
        # Pattern: "Eotchorts FG-X d1-%" matches systems like "Eotchorts FG-X d1-318"
        sector = params.sector.strip()
        sub_sector = params.sub_sector.strip()
        mass_code = params.mass_code.strip()
        like = f"{sector} {sub_sector} {mass_code}-%"
        cursor = source.execute(
            f"""
            SELECT {name_col}, {x_col}, {y_col}, {z_col}
            FROM {table_name}
            WHERE LOWER({name_col}) LIKE LOWER(?)
            """,
            (like,),
        )
        rows = cursor.fetchall()
        dest.executemany(
            "INSERT OR REPLACE INTO working_systems(system_name, x, y, z) VALUES (?, ?, ?, ?)",
            rows,
        )
        inserted = len(rows)
        _emit(
            progress_cb,
            JobProgress(
                phase="extract",
                current=inserted,
                total=inserted,
                message=f"Sub-sector systems: {inserted}",
            ),
        )

    dest.commit()
    return inserted


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _resolve_sector_db_path(params: JobParams) -> str:
    override = get_sector_db_override()
    if override:
        path = Path(override)
        if path.exists():
            return str(path)
        raise ValueError(f"Sector DB not found: {path}. Run extractor to create it.")
    sector_name = params.sector or ""
    if not sector_name and params.center_system:
        sector_name = _sector_from_system_name(params.center_system)
    if sector_name:
        path = Path(resolve_sector_db_path(sector_name))
        if path.exists():
            return str(path)
        raise ValueError(f"Sector DB not found: {path}. Run extractor to create it.")
    path = Path(resolve_sector_db_path("unknown"))
    raise ValueError(f"Sector DB not found: {path}. Run extractor to create it.")


def _count_systems(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) FROM systems").fetchone()
    except sqlite3.Error as exc:
        raise ValueError("Systems table not found in sector DB") from exc
    return int(row[0] if row else 0)


def _sector_from_system_name(system_name: str) -> str:
    sector = detect_sector_from_system(system_name or "")
    if not sector or sector == "Unknown":
        return ""
    return sector


def _emit(progress_cb: Optional[callable], progress: JobProgress) -> None:
    if progress_cb:
        progress_cb(progress)

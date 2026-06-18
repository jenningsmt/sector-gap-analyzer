"""SQLite helpers for the strategic planner working database."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from planner_strategic.models import Candidate


def connect_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def ensure_working_db(path: str) -> None:
    conn = connect_db(path)
    try:
        _create_tables(conn)
    finally:
        conn.close()


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edsm_cache(
            system_name TEXT PRIMARY KEY,
            "exists" INTEGER NOT NULL,
            checked_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategic_candidates(
            system_name TEXT PRIMARY KEY,
            score REAL NOT NULL,
            confidence TEXT NOT NULL,
            distance_ly REAL,
            tags TEXT NOT NULL,
            rationale TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategic_runs(
            run_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            params_json TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            kept INTEGER NOT NULL,
            removed INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategic_seed(
            mode TEXT NOT NULL,
            sector TEXT,
            center_system TEXT,
            radius_ly REAL,
            system_name TEXT NOT NULL,
            base_score REAL,
            distance_ly REAL,
            tags TEXT,
            rationale TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS working_systems(
            system_name TEXT PRIMARY KEY,
            x REAL NOT NULL,
            y REAL NOT NULL,
            z REAL NOT NULL
        )
        """
    )
    conn.commit()


def replace_candidates(
    conn: sqlite3.Connection,
    candidates: Iterable[Candidate],
    created_at: str,
) -> None:
    conn.execute("DELETE FROM strategic_candidates")
    conn.executemany(
        """
        INSERT OR REPLACE INTO strategic_candidates(
            system_name, score, confidence, distance_ly, tags, rationale, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                candidate.system_name,
                candidate.score,
                candidate.confidence,
                candidate.distance_ly,
                json.dumps(candidate.tags),
                candidate.rationale,
                created_at,
            )
            for candidate in candidates
        ],
    )
    conn.commit()

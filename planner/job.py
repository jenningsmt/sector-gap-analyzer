"""Strategic planner job orchestration."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from planner_strategic import candidates, db, extract
from planner_strategic.edsm import EDSMClient, filter_undiscovered
from planner_strategic.models import Candidate, JobParams, JobProgress


def run_strategic_plan(
    params: JobParams,
    *,
    working_db_path: str,
    progress_cb: Optional[callable] = None,
    edsm_client: Optional[object] = None,
    invalid_systems: Optional[set[str]] = None,
) -> list[Candidate]:
    _emit(progress_cb, JobProgress(phase="extract", current=0, total=0, message="Starting"))
    db.ensure_working_db(working_db_path)
    extract.extract_minimal(working_db_path, params, progress_cb)
    _assert_working_systems(working_db_path)

    _emit(progress_cb, JobProgress(phase="generate", current=0, total=0, message="Generating candidates"))
    if params.mode == "gap":
        generated = candidates.generate_candidates_gap(
            working_db_path,
            params.sector,
            count=200,
            progress_cb=progress_cb,
        )
    elif params.mode == "spatial":
        generated = candidates.generate_candidates_spatial(
            working_db_path, params.center_system, params.radius_ly, progress_cb
        )
    elif params.mode == "hot_subsector":
        generated = candidates.generate_candidates_hot_subsector(
            working_db_path,
            params.sector,
            params.sub_sector,
            params.mass_code,
            count=200,
            progress_cb=progress_cb,
        )
    else:
        raise ValueError("Unsupported mode")

    _emit(progress_cb, JobProgress(phase="validate", current=0, total=len(generated), message="Validating"))
    client = edsm_client or EDSMClient()
    validated = filter_undiscovered(
        generated,
        working_db_path,
        client,
        progress_cb,
        cache_max_age_days=0,  # Force fresh EDSM checks on each run to catch recently discovered systems
        min_keep=25 if params.mode in {"gap", "spatial", "hot_subsector"} else 0,
        max_checks=500 if params.mode == "spatial" else 300,
        invalid_systems=invalid_systems,
    )
    _emit(
        progress_cb,
        JobProgress(
            phase="validate",
            current=len(validated),
            total=len(generated),
            message=f"Candidates kept: {len(validated)}",
        ),
    )

    _emit(progress_cb, JobProgress(phase="persist", current=0, total=len(validated), message="Persisting"))
    _persist_run(params, generated, validated, working_db_path)

    _emit(progress_cb, JobProgress(phase="done", current=len(validated), total=len(validated), message="Done"))
    return validated


def _assert_working_systems(working_db_path: str) -> None:
    conn = sqlite3.connect(working_db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM working_systems").fetchone()
        count = row[0] if row else 0
    finally:
        conn.close()
    if count <= 0:
        raise ValueError("working_systems is empty after extraction")


def _persist_run(
    params: JobParams,
    generated: list[Candidate],
    validated: list[Candidate],
    working_db_path: str,
) -> None:
    created_at = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid4())
    conn = sqlite3.connect(working_db_path)
    try:
        db.replace_candidates(conn, validated, created_at)
        conn.execute(
            """
            INSERT OR REPLACE INTO strategic_runs(
                run_id, mode, params_json, started_at, finished_at, kept, removed
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                params.mode,
                json.dumps(
                    {
                        "sector": params.sector,
                        "center_system": params.center_system,
                        "radius_ly": params.radius_ly,
                        "sub_sector": params.sub_sector,
                        "mass_code": params.mass_code,
                        "generated": len(generated),
                    }
                ),
                created_at,
                created_at,
                len(validated),
                max(len(generated) - len(validated), 0),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _emit(progress_cb: Optional[callable], progress: JobProgress) -> None:
    if progress_cb:
        progress_cb(progress)

"""Orchestrates the gap-analysis pipeline for the GUI: per selected sector,
extract -> bracketed gaps -> backward (and optionally forward) extrapolation,
then aggregate all sectors into one master candidate list.

Each stage is a thin call into the existing scripts/ modules' run() functions
-- no pipeline logic is duplicated here, only sequencing, logging, and
cooperative-cancellation checks between stages.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from scripts import (
    aggregate_gap_master_list,
    extract_multi_sector_to_sqlite,
    gap_extrapolate_export,
    gap_full_export,
)
from scripts.extract_sector_systems_to_sqlite import sanitize_prefix


def sector_db_path(project_dir: Path, sector: str) -> Path:
    return project_dir / "data" / "sector_library" / f"sector_{sanitize_prefix(sector)}.sqlite"


def _cancelled(cancel_event: Optional[threading.Event]) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def run_pipeline(config: dict[str, Any], cancel_event: Optional[threading.Event] = None) -> int:
    """Run the configured stages for config["sectors"]. Returns 0 on normal
    completion, 130 if cancelled partway through."""
    project_dir = Path(config["project_dir"])
    galaxy_dump_path = Path(config["galaxy_dump_path"])
    out_dir = project_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    sectors: list[str] = list(config.get("sectors") or [])
    stages: dict[str, bool] = config.get("stages") or {}
    dry_run: bool = bool(config.get("dry_run", True))

    if not sectors:
        print("No sectors selected. Nothing to do.")
        return 1

    print(f"=== Sector Gap Analyzer pipeline: {len(sectors)} sector(s) ===")
    print(f"  Project dir : {project_dir}")
    print(f"  Galaxy dump : {galaxy_dump_path}")
    print(f"  Sectors     : {', '.join(sectors)}")
    print(f"  Dry run     : {dry_run}")
    print()

    # -------------------------------------------------------------------
    # Stage: extraction (single pass covering every selected sector)
    # -------------------------------------------------------------------
    if stages.get("extract", True):
        print(">>> Stage: extraction")
        rc = extract_multi_sector_to_sqlite.run(
            input_path=galaxy_dump_path,
            sector_prefixes=sectors,
            output_dir=project_dir / "data" / "sector_library",
            cancel_event=cancel_event,
        )
        if rc == 130 or _cancelled(cancel_event):
            print("Cancelled during extraction.")
            return 130
        if rc != 0:
            print(f"Extraction failed (return code {rc}); stopping.")
            return rc
        print()

    if _cancelled(cancel_event):
        print("Cancelled before gap analysis stages.")
        return 130

    # -------------------------------------------------------------------
    # Per-sector: bracketed gaps + backward/forward extrapolation
    # -------------------------------------------------------------------
    for sector in sectors:
        if _cancelled(cancel_event):
            print("Cancelled; skipping remaining sectors.")
            return 130

        db_path = sector_db_path(project_dir, sector)
        if not db_path.exists():
            print(f"SKIP {sector!r}: no sector DB found at {db_path} (run extraction first).")
            continue

        print(f">>> Sector: {sector}")

        if stages.get("bracketed_gaps", True):
            print(f"  -- Bracketed gaps ({sector}) --")
            gap_full_export.run(
                db_path=db_path,
                sector=sector,
                out_dir=out_dir,
                dry_run=dry_run,
                cache_db_path=None,
                max_bracket_width=config.get("max_bracket_width", 25),
                cancel_event=cancel_event,
            )
            if _cancelled(cancel_event):
                return 130

        run_backward = stages.get("backward_extrap", True)
        run_forward = stages.get("forward_extrap", False)
        if run_backward or run_forward:
            direction = "both" if (run_backward and run_forward) else (
                "forward" if run_forward else "backward"
            )
            print(f"  -- Extrapolation ({sector}, direction={direction}) --")
            gap_extrapolate_export.run(
                db_path=db_path,
                sector=sector,
                out_dir=out_dir,
                extend_depth=config.get("extend_depth", 5),
                direction=direction,
                max_forward_step=config.get("max_forward_step", 5),
                dry_run=dry_run,
                cache_db_path=None,
                cancel_event=cancel_event,
            )
            if _cancelled(cancel_event):
                return 130

        print()

    if _cancelled(cancel_event):
        print("Cancelled before aggregation.")
        return 130

    # -------------------------------------------------------------------
    # Stage: aggregation across all sectors present in out_dir
    # -------------------------------------------------------------------
    if stages.get("aggregate", True):
        print(">>> Stage: aggregation")
        rows = aggregate_gap_master_list.load_rows(out_dir, sector_filter=None)
        rows.sort(key=aggregate_gap_master_list.sort_key)
        if not rows:
            print("  No not_in_edsm candidate rows found. Nothing to aggregate.")
        else:
            sectors_found = sorted({row.sector_slug for row in rows})
            print(f"  {len(rows)} candidate rows across {len(sectors_found)} sector(s)")
            csv_path = out_dir / "master_gap_candidates.csv"
            md_path = out_dir / "master_gap_candidates.md"
            aggregate_gap_master_list.write_master_csv(csv_path, rows)
            aggregate_gap_master_list.write_master_md(md_path, rows)

    print("\n=== Pipeline complete ===")
    return 0

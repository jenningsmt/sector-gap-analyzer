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
    gap_naming,
    gap_spatial_export,
    sector_summary_export,
)
from scripts.extract_sector_systems_to_sqlite import sanitize_prefix


def sector_db_path(project_dir: Path, sector: str) -> Path:
    return project_dir / "data" / "sector_library" / f"sector_{sanitize_prefix(sector)}.sqlite"


def _cancelled(cancel_event: Optional[threading.Event]) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _detect_sector_from_system(system_name: str) -> str:
    """Best-effort sector name from a procedural system name: everything
    before the subsector token, e.g. 'Heart Sector AA-Q b5-3' -> 'Heart Sector'.
    Returns '' if the name doesn't parse (e.g. a named system like 'Sol')."""
    tokens = [t for t in system_name.split() if t]
    idx = gap_naming.find_subsector_index(tokens)
    if idx is None or idx == 0:
        return ""
    return " ".join(tokens[:idx])


def run_pipeline(config: dict[str, Any], cancel_event: Optional[threading.Event] = None) -> int:
    """Dispatch to the sector-prefix ("gap") or radius-around-a-system
    ("spatial") pipeline based on config["mode"]. Returns 0 on normal
    completion, 130 if cancelled partway through, 1 on a config/setup error."""
    mode = config.get("mode", "gap")
    if mode == "spatial":
        return _run_spatial_pipeline(config, cancel_event)
    return _run_gap_pipeline(config, cancel_event)


def _run_gap_pipeline(config: dict[str, Any], cancel_event: Optional[threading.Event] = None) -> int:
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

        print(">>> Stage: sector summaries")
        for sector in sectors:
            if _cancelled(cancel_event):
                break
            db_path = sector_db_path(project_dir, sector)
            if not db_path.exists():
                continue
            try:
                summary = sector_summary_export.analyze_sector(str(db_path), sector)
                if summary is None:
                    print(f"  SKIP summary for {sector!r}: analysis returned no data.")
                    continue
                out_path = sector_summary_export.summary_path(project_dir, sector)
                sector_summary_export.generate_summary_report(summary, out_path)
                print(f"  {sector}: summary written to {out_path}")
            except Exception as exc:
                print(f"  SKIP summary for {sector!r}: {exc}")
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
            try:
                gap_full_export.run(
                    db_path=db_path,
                    sector=sector,
                    out_dir=out_dir,
                    dry_run=dry_run,
                    cache_db_path=None,
                    max_bracket_width=config.get("max_bracket_width", 25),
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                print(f"  SKIP bracketed gaps for {sector!r}: {exc}")
            if _cancelled(cancel_event):
                return 130

        run_backward = stages.get("backward_extrap", True)
        run_forward = stages.get("forward_extrap", False)
        if run_backward or run_forward:
            direction = "both" if (run_backward and run_forward) else (
                "forward" if run_forward else "backward"
            )
            print(f"  -- Extrapolation ({sector}, direction={direction}) --")
            try:
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
            except Exception as exc:
                print(f"  SKIP extrapolation for {sector!r}: {exc}")
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
        _run_aggregation(out_dir)

    print("\n=== Pipeline complete ===")
    return 0


def _run_aggregation(out_dir: Path) -> None:
    print(">>> Stage: aggregation")
    rows = aggregate_gap_master_list.load_rows(out_dir, sector_filter=None)
    rows.sort(key=aggregate_gap_master_list.sort_key)
    if not rows:
        print("  No not_in_edsm candidate rows found. Nothing to aggregate.")
        return
    sectors_found = sorted({row.sector_slug for row in rows})
    print(f"  {len(rows)} candidate rows across {len(sectors_found)} sector(s)")
    csv_path = out_dir / "master_gap_candidates.csv"
    md_path = out_dir / "master_gap_candidates.md"
    aggregate_gap_master_list.write_master_csv(csv_path, rows)
    aggregate_gap_master_list.write_master_md(md_path, rows)


def _run_spatial_pipeline(config: dict[str, Any], cancel_event: Optional[threading.Event] = None) -> int:
    """Run a radius-around-a-system gap search. Unlike the sector pipeline,
    this never triggers galaxy-dump extraction -- it requires the sector
    containing the center system to have already been extracted."""
    project_dir = Path(config["project_dir"])
    out_dir = project_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    center_system: str = str(config.get("spatial_center_system") or "").strip()
    radius_ly = config.get("spatial_radius_ly", 20)
    sector_override: str = str(config.get("spatial_sector_override") or "").strip()
    stages: dict[str, bool] = config.get("stages") or {}
    dry_run: bool = bool(config.get("dry_run", True))

    if not center_system:
        print("No center system given. Nothing to do.")
        return 1
    try:
        radius_ly = float(radius_ly)
    except (TypeError, ValueError):
        print(f"Invalid radius: {radius_ly!r}")
        return 1
    if radius_ly <= 0:
        print("Radius must be > 0.")
        return 1

    sector = sector_override or _detect_sector_from_system(center_system)
    if not sector:
        print(
            f"Could not detect a sector from center system {center_system!r}. "
            "Set the sector override field explicitly."
        )
        return 1

    db_path = sector_db_path(project_dir, sector)
    if not db_path.exists():
        print(
            f"Sector DB not found: {db_path}. Spatial search requires the sector "
            f"containing the center system to be extracted first (switch to Gap "
            f"mode, add {sector!r}, and run extraction)."
        )
        return 1

    print("=== Sector Gap Analyzer pipeline: spatial search ===")
    print(f"  Project dir   : {project_dir}")
    print(f"  Sector        : {sector}")
    print(f"  Center system : {center_system}")
    print(f"  Radius        : {radius_ly} ly")
    print(f"  Dry run       : {dry_run}")
    print()

    try:
        gap_spatial_export.run(
            db_path=db_path,
            sector=sector,
            center_system=center_system,
            radius_ly=radius_ly,
            out_dir=out_dir,
            dry_run=dry_run,
            cache_db_path=None,
            max_bracket_width=config.get("max_bracket_width", 25),
            extend_depth=config.get("extend_depth", 5),
            run_bracketed=stages.get("bracketed_gaps", True),
            run_backward=stages.get("backward_extrap", True),
            cancel_event=cancel_event,
        )
    except Exception as exc:
        print(f"Spatial search failed: {exc}")
        return 1

    if _cancelled(cancel_event):
        print("Cancelled before aggregation.")
        return 130

    if stages.get("aggregate", True):
        _run_aggregation(out_dir)

    print("\n=== Pipeline complete ===")
    return 0

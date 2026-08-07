"""Orchestrates the gap-analysis pipeline for the GUI: per selected sector,
extract -> bracketed gaps -> backward (and optionally forward) extrapolation,
then aggregate all sectors into one master candidate list.

Each stage is a thin call into the existing scripts/ modules' run() functions
-- no pipeline logic is duplicated here, only sequencing, logging, and
cooperative-cancellation checks between stages.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from scripts import (
    aggregate_gap_master_list,
    bio_opportunity_export,
    extract_multi_sector_to_sqlite,
    gap_extrapolate_export,
    gap_full_export,
    gap_naming,
    gap_spatial_export,
    sector_summary_export,
)
from scripts.extract_sector_systems_to_sqlite import sanitize_prefix

# EDSM validation is throttled to 1 request/second (see scripts/gap_full_export.py
# and gap_extrapolate_export.py), so estimated candidate count == estimated seconds.
# This is a worst case that ignores existing EDSM-result cache hits, which is
# deliberate: it's only used to decide whether to show the confirmation prompt,
# and actual runs are often faster than this once a sector has been run before.
LARGE_RUN_WARN_SECONDS = 3600


def sector_db_path(project_dir: Path, sector: str) -> Path:
    return project_dir / "data" / "sector_library" / f"sector_{sanitize_prefix(sector)}.sqlite"


def _cancelled(cancel_event: Optional[threading.Event]) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _load_sector_system_names(db_path: Path, sector: str) -> list[str]:
    like = sector.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + " %"
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM systems WHERE LOWER(name) LIKE LOWER(?) ESCAPE '\\'",
            (like,),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _estimate_candidates_from_names(
    names: list[str],
    stages: dict[str, bool],
    config: dict[str, Any],
    *,
    include_forward: bool = True,
) -> dict[str, int]:
    """Local-only (no network) candidate counts for each enabled EDSM-bound
    stage, computed with the exact same functions those stages use to
    generate candidates -- so these numbers match what would actually run."""
    known = set(names)
    per_stage: dict[str, int] = {}

    if stages.get("bracketed_gaps", True):
        sequences = gap_full_export.build_sequences(names)
        raw = gap_full_export.generate_bracketed_gaps(
            sequences, max_bracket_width=config.get("max_bracket_width", 25)
        )
        per_stage["Bracketed gaps"] = len(set(raw) - known)

    if stages.get("backward_extrap", True):
        bwd = gap_extrapolate_export.build_backward_candidates(
            names, config.get("extend_depth", 5)
        )
        per_stage["Backward extrapolation"] = len(bwd)

    if include_forward and stages.get("forward_extrap", False):
        fwd = gap_extrapolate_export.build_forward_step1_candidates(names)
        per_stage["Forward extrapolation (step 1 only; chain steps not estimated)"] = len(fwd)

    return per_stage


def _estimate_edsm_stage_candidates(
    db_path: Path, sector: str, stages: dict[str, bool], config: dict[str, Any]
) -> dict[str, Any]:
    names = _load_sector_system_names(db_path, sector)
    per_stage = _estimate_candidates_from_names(names, stages, config)
    total = sum(per_stage.values())
    return {
        "sector": sector,
        "known_systems": len(names),
        "per_stage": per_stage,
        "total_candidates": total,
        "worst_case_seconds": total,  # 1 req/s
    }


def _estimate_spatial_candidates(
    db_path: Path, center_system: str, radius_ly: float, stages: dict[str, bool], config: dict[str, Any]
) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    try:
        resolved_name, cx, cy, cz = gap_spatial_export.resolve_center(conn, center_system)
        names = gap_spatial_export.load_neighborhood(conn, cx, cy, cz, radius_ly)
    finally:
        conn.close()
    per_stage = _estimate_candidates_from_names(names, stages, config, include_forward=False)
    total = sum(per_stage.values())
    return {
        "sector": f"{resolved_name} (within {radius_ly} ly)",
        "known_systems": len(names),
        "per_stage": per_stage,
        "total_candidates": total,
        "worst_case_seconds": total,
    }


def _format_large_run_message(estimate: dict[str, Any]) -> str:
    lines = [
        f"{estimate['sector']!r} looks large: {estimate['known_systems']:,} known systems.",
        "",
    ]
    for label, count in estimate["per_stage"].items():
        hours = count / 3600
        lines.append(f"  {label}: {count:,} candidates (~{hours:.1f}h worst case)")
    total = estimate["total_candidates"]
    total_hours = estimate["worst_case_seconds"] / 3600
    lines.append("")
    lines.append(
        f"Total: {total:,} candidates, ~{total_hours:.1f}h worst case "
        "at EDSM's 1 request/second limit."
    )
    lines.append("")
    lines.append(
        "Actual time may be shorter if some candidates are already cached "
        "from a previous run (EDSM results cache for 7 days)."
    )
    lines.append("")
    lines.append("Continue with EDSM-validated stages for this sector?")
    return "\n".join(lines)


def _confirm_large_run(
    confirm_cb: Optional[Callable[[dict], bool]],
    cancel_event: Optional[threading.Event],
    estimate: dict[str, Any],
) -> bool:
    """Returns True to proceed. If the estimate is below the warning
    threshold, or there's no confirm_cb wired up (e.g. CLI usage), proceeds
    without prompting."""
    if estimate["worst_case_seconds"] < LARGE_RUN_WARN_SECONDS:
        return True
    if confirm_cb is None:
        return True
    message = _format_large_run_message(estimate)
    print(f"\n  {message}\n")
    proceed = confirm_cb({"message": message, **estimate})
    if not proceed:
        print(
            f"  Skipping EDSM-validated stages for {estimate['sector']!r} by user choice "
            f"({estimate['total_candidates']:,} candidates, "
            f"~{estimate['worst_case_seconds'] / 3600:.1f}h worst case)."
        )
    return proceed


def _detect_sector_from_system(system_name: str) -> str:
    """Best-effort sector name from a procedural system name: everything
    before the subsector token, e.g. 'Heart Sector AA-Q b5-3' -> 'Heart Sector'.
    Returns '' if the name doesn't parse (e.g. a named system like 'Sol')."""
    tokens = [t for t in system_name.split() if t]
    idx = gap_naming.find_subsector_index(tokens)
    if idx is None or idx == 0:
        return ""
    return " ".join(tokens[:idx])


def run_pipeline(
    config: dict[str, Any],
    cancel_event: Optional[threading.Event] = None,
    confirm_cb: Optional[Callable[[dict], bool]] = None,
) -> int:
    """Dispatch to the sector-prefix ("gap") or radius-around-a-system
    ("spatial") pipeline based on config["mode"]. Returns 0 on normal
    completion, 130 if cancelled partway through, 1 on a config/setup error.

    confirm_cb, if given, is called with a payload dict (see
    _format_large_run_message) whenever a sector's estimated EDSM-validated
    candidate volume exceeds LARGE_RUN_WARN_SECONDS, and must return True to
    proceed or False to skip that sector's EDSM-bound stages. It's expected
    to block the calling thread until answered (see gui/worker.py) -- when
    None (e.g. running scripts directly, not through the GUI), large runs
    proceed without prompting."""
    mode = config.get("mode", "gap")
    if mode == "spatial":
        return _run_spatial_pipeline(config, cancel_event, confirm_cb)
    return _run_gap_pipeline(config, cancel_event, confirm_cb)


def _run_gap_pipeline(
    config: dict[str, Any],
    cancel_event: Optional[threading.Event] = None,
    confirm_cb: Optional[Callable[[dict], bool]] = None,
) -> int:
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

    print(f"=== ED Sector Surveyor pipeline: {len(sectors)} sector(s) ===")
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

        skip_edsm_stages = False
        edsm_stages_enabled = (
            stages.get("bracketed_gaps", True)
            or stages.get("backward_extrap", True)
            or stages.get("forward_extrap", False)
        )
        if edsm_stages_enabled and not dry_run:
            try:
                estimate = _estimate_edsm_stage_candidates(db_path, sector, stages, config)
                skip_edsm_stages = not _confirm_large_run(confirm_cb, cancel_event, estimate)
            except Exception as exc:
                print(f"  (could not estimate run time for {sector!r}: {exc})")
            if _cancelled(cancel_event):
                print("Cancelled; skipping remaining sectors.")
                return 130

        if stages.get("bracketed_gaps", True) and not skip_edsm_stages:
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

        run_backward = stages.get("backward_extrap", True) and not skip_edsm_stages
        run_forward = stages.get("forward_extrap", False) and not skip_edsm_stages
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

        if stages.get("bio_opportunity", False):
            print(f"  -- Stale exobiology candidates ({sector}) --")
            try:
                bio_opportunity_export.run(db_path=db_path, sector=sector, out_dir=out_dir)
            except Exception as exc:
                print(f"  SKIP bio opportunity export for {sector!r}: {exc}")
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


def _run_spatial_pipeline(
    config: dict[str, Any],
    cancel_event: Optional[threading.Event] = None,
    confirm_cb: Optional[Callable[[dict], bool]] = None,
) -> int:
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

    print("=== ED Sector Surveyor pipeline: spatial search ===")
    print(f"  Project dir   : {project_dir}")
    print(f"  Sector        : {sector}")
    print(f"  Center system : {center_system}")
    print(f"  Radius        : {radius_ly} ly")
    print(f"  Dry run       : {dry_run}")
    print()

    run_bracketed = stages.get("bracketed_gaps", True)
    run_backward = stages.get("backward_extrap", True)
    if (run_bracketed or run_backward) and not dry_run:
        try:
            estimate = _estimate_spatial_candidates(db_path, center_system, radius_ly, stages, config)
            if not _confirm_large_run(confirm_cb, cancel_event, estimate):
                run_bracketed = False
                run_backward = False
        except Exception as exc:
            print(f"  (could not estimate run time: {exc})")
        if _cancelled(cancel_event):
            print("Cancelled.")
            return 130

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
            run_bracketed=run_bracketed,
            run_backward=run_backward,
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

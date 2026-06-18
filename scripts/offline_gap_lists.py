"""Offline gap list generator (separate from UI, still validates via EDSM).

Creates gap/spatial/hot-subsector lists using local sector DBs plus EDSM validation.
Intended for sharing hypotheses with other Commanders for manual validation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import uuid

from app.config import get_app_data_dir
from planner_strategic import candidates, db, extract
from planner_strategic.edsm import EDSMClient, filter_undiscovered
from planner_strategic.models import JobParams


def _load_invalid_systems() -> set[str]:
    invalid_file = get_app_data_dir() / "invalid_systems.txt"
    if not invalid_file.exists():
        return set()
    return {line.strip() for line in invalid_file.read_text(encoding="utf-8").splitlines() if line.strip()}


def _validate_candidates(
    generated: list[candidates.Candidate],
    mode: str,
    working_db_path: str,
    invalid_systems: set[str],
    *,
    min_keep: int,
    max_checks: int,
) -> list[candidates.Candidate]:
    client = EDSMClient()
    return filter_undiscovered(
        generated,
        working_db_path,
        client,
        progress_cb=None,
        cache_max_age_days=0,
        min_keep=min_keep,
        max_checks=max_checks,
        invalid_systems=invalid_systems,
    )


def _generate_spatial_candidates_200(working_db_path: str) -> list[candidates.Candidate]:
    systems = candidates._load_working_systems(working_db_path)
    if not systems:
        raise ValueError("No neighborhood systems available for spatial planning")
    families = candidates._build_families([row["system_name"] for row in systems])
    top_families = candidates._top_families(families, top_k=10)
    desired = 200
    existing: set[str] = set()
    generated: list[candidates.Candidate] = []
    per_family = int((desired + max(len(top_families), 1) - 1) / max(len(top_families), 1))
    for family_key in top_families:
        batch = candidates._generate_family_candidates(
            family_key,
            families[family_key],
            per_family,
            existing,
            tags=["SPATIAL", "HYPOTHESIS"],
            rationale_prefix="SPATIAL FAMILY",
        )
        generated.extend(batch)
        existing.update([cand.system_name for cand in batch])
    scored = candidates._score_candidates(generated, families)
    ordered = candidates._apply_dispersion(scored)
    return ordered[:desired]


def _write_list(path: Path, title: str, params: str, candidates_list: list[candidates.Candidate]) -> None:
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"Params: {params}")
    lines.append(f"Count: {len(candidates_list)}")
    lines.append("")
    for idx, cand in enumerate(candidates_list, 1):
        lines.append(f"{idx:3d}. {cand.system_name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_with_temp_db(params: JobParams, generator_fn) -> tuple[list[candidates.Candidate], Path]:
    temp_db = get_app_data_dir() / f"working_{uuid.uuid4().hex}.sqlite"
    db.ensure_working_db(str(temp_db))
    extract.extract_minimal(str(temp_db), params, progress_cb=None)
    generated = generator_fn(str(temp_db))
    return generated, temp_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline gap list generator (EDSM-validated, separate from UI)")
    parser.add_argument("--sector", required=True, help="Sector name (e.g., Thuecheae)")
    parser.add_argument("--center", help="Spatial center system name")
    parser.add_argument("--radius", type=float, default=0.0, help="Spatial radius (ly)")
    parser.add_argument("--sub-sector", dest="sub_sector", action="append", default=[], help="Hot subsector code")
    parser.add_argument("--mass-code", dest="mass_code", action="append", default=[], help="Mass code (e.g., d13)")
    parser.add_argument("--out-dir", default=str(get_app_data_dir() / "sector_library"), help="Output directory")
    parser.add_argument("--min-keep", type=int, default=0, help="Minimum kept before stopping validation (0 = no early stop)")
    parser.add_argument("--max-checks", type=int, default=1000, help="Max EDSM checks per list")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    invalid_systems = _load_invalid_systems()

    # 1) Gap list (200)
    params_gap = JobParams(mode="gap", sector=args.sector)
    gap_generated, gap_db = _run_with_temp_db(
        params_gap,
        lambda db_path: candidates.generate_candidates_gap(db_path, params_gap.sector, count=200, progress_cb=None),
    )
    gap_validated = _validate_candidates(
        gap_generated,
        "gap",
        str(gap_db),
        invalid_systems,
        min_keep=args.min_keep,
        max_checks=args.max_checks,
    )
    try:
        gap_db.unlink(missing_ok=True)
    except Exception:
        pass
    _write_list(
        out_dir / f"{args.sector.lower()}_gap_200_validated.md",
        f"{args.sector} Gap Systems (Top 200, EDSM-validated)",
        f"mode=gap sector={args.sector} count=200 min_keep={args.min_keep} max_checks={args.max_checks}",
        gap_validated,
    )

    # 2) Spatial list (<=200 within radius)
    if args.center and args.radius and args.radius > 0:
        params_spatial = JobParams(mode="spatial", center_system=args.center, radius_ly=args.radius)
        spatial_generated, spatial_db = _run_with_temp_db(
            params_spatial, lambda db_path: _generate_spatial_candidates_200(db_path)
        )
        spatial_validated = _validate_candidates(
            spatial_generated,
            "spatial",
            str(spatial_db),
            invalid_systems,
            min_keep=args.min_keep,
            max_checks=args.max_checks,
        )
        try:
            spatial_db.unlink(missing_ok=True)
        except Exception:
            pass
        _write_list(
            out_dir / f"{args.sector.lower()}_spatial_{args.center.replace(' ', '_')}_{int(args.radius)}ly_validated.md",
            f"{args.sector} Spatial Gap Systems (<=200 within {args.radius} ly of {args.center}, EDSM-validated)",
            f"mode=spatial center={args.center} radius_ly={args.radius} count=200 min_keep={args.min_keep} max_checks={args.max_checks}",
            spatial_validated,
        )

    # 3) Hot subsector list(s)
    if args.sub_sector and args.mass_code:
        pairs = list(zip(args.sub_sector, args.mass_code))
        lines: list[str] = []
        lines.append(f"# {args.sector} Hot Sub-Sector Gap Systems (EDSM-validated)")
        lines.append("")
        lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
        lines.append("")
        for sub_sector, mass_code in pairs:
            params_hot = JobParams(
                mode="hot_subsector",
                sector=args.sector,
                sub_sector=sub_sector,
                mass_code=mass_code,
            )
            hot_generated, hot_db = _run_with_temp_db(
                params_hot,
                lambda db_path, p=params_hot: candidates.generate_candidates_hot_subsector(
                    db_path,
                    p.sector,
                    p.sub_sector,
                    p.mass_code,
                    count=200,
                    progress_cb=None,
                ),
            )
            hot_validated = _validate_candidates(
                hot_generated,
                "hot_subsector",
                str(hot_db),
                invalid_systems,
                min_keep=args.min_keep,
                max_checks=args.max_checks,
            )
            try:
                hot_db.unlink(missing_ok=True)
            except Exception:
                pass
            lines.append(f"## {args.sector} {sub_sector} {mass_code}")
            lines.append(f"Count: {len(hot_validated)}")
            lines.append("")
            for idx, cand in enumerate(hot_validated, 1):
                lines.append(f"{idx:3d}. {cand.system_name}")
            lines.append("")

        hot_path = out_dir / f"{args.sector.lower()}_hot_subsector_validated.md"
        hot_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
sector_summary_export.py — Sector baseline summary report.
=============================================================================
Port of edmfi-mfi's app/sector_summary_analyzer.py, adapted for this repo's
standalone sector_library SQLite databases (schema is identical: systems /
bodies / rings tables with the same column names and raw_json shape).

Analyzes a sector DB (ring density baseline, Earth-like world and
bio-signature hotspots, hot subsector patterns) and writes a Markdown
summary report. Called automatically by gui/pipeline.py right after a
sector finishes extracting; also runnable standalone.

Usage:
  python scripts/sector_summary_export.py \\
      --db data/sector_library/sector_heart_sector.sqlite \\
      --sector "Heart Sector"
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from scripts.extract_sector_systems_to_sqlite import sanitize_prefix
except ImportError:
    from extract_sector_systems_to_sqlite import sanitize_prefix


@dataclass
class SectorSummary:
    """Summary statistics and intelligence for a sector."""
    sector_name: str
    total_systems: int
    total_bodies: int
    total_rings: int

    # Ring density baseline
    ring_stats: dict[str, float]

    # ELW analysis
    total_elws: int
    elw_systems: int
    hot_elw_patterns: list[tuple[str, int, int]]  # (pattern, systems, elws)

    # Bio-signature analysis
    total_bio_systems: int
    high_bio_systems: int  # 5+ signatures
    hot_bio_patterns: list[tuple[str, int, int]]  # (pattern, systems, total_sigs)

    # Overall statistics
    analyzed_at: str


def summary_path(project_dir: Path, sector: str) -> Path:
    """data/sector_library/sector_<slug>_summary.md -- same directory and
    slug convention as the sector's .sqlite file."""
    return project_dir / "data" / "sector_library" / f"sector_{sanitize_prefix(sector)}_summary.md"


def analyze_sector(sector_db_path: str, sector_name: str) -> Optional[SectorSummary]:
    """Analyze a sector database and return summary statistics.

    Args:
        sector_db_path: Path to the sector SQLite database
        sector_name: Name of the sector

    Returns:
        SectorSummary with analysis results, or None if analysis fails
    """
    try:
        conn = sqlite3.connect(sector_db_path)

        # Overall statistics
        total_systems = _count_systems(conn)
        total_bodies = _count_bodies(conn)
        total_rings = _count_rings(conn)

        # Ring density statistics
        ring_stats = _calculate_ring_stats(conn)

        # ELW analysis
        elw_systems_data = _analyze_elws(conn)
        total_elws = sum(count for _, count in elw_systems_data)
        elw_systems = len(elw_systems_data)
        hot_elw_patterns = _group_by_pattern(elw_systems_data, top_k=10)

        # Bio-signature analysis
        bio_systems_data = _analyze_biosigs(conn)
        total_bio_systems = len(bio_systems_data)
        high_bio_systems = sum(1 for _, count in bio_systems_data if count >= 5)
        hot_bio_patterns = _group_by_pattern(bio_systems_data, top_k=10, min_value=5)

        analyzed_at = datetime.now(timezone.utc).isoformat()

        conn.close()

        return SectorSummary(
            sector_name=sector_name,
            total_systems=total_systems,
            total_bodies=total_bodies,
            total_rings=total_rings,
            ring_stats=ring_stats,
            total_elws=total_elws,
            elw_systems=elw_systems,
            hot_elw_patterns=hot_elw_patterns,
            total_bio_systems=total_bio_systems,
            high_bio_systems=high_bio_systems,
            hot_bio_patterns=hot_bio_patterns,
            analyzed_at=analyzed_at,
        )
    except Exception as e:
        print(f"Error analyzing sector: {e}")
        return None


def generate_summary_report(summary: SectorSummary, output_path: Path) -> None:
    """Generate a markdown summary report."""
    lines = []
    lines.append(f"# Sector Baseline Summary: {summary.sector_name}")
    lines.append(f"\nGenerated: {summary.analyzed_at}")
    lines.append("\n---\n")

    # Overall Statistics
    lines.append("## Sector Overview")
    lines.append(f"- **Total Systems**: {summary.total_systems:,}")
    lines.append(f"- **Total Bodies**: {summary.total_bodies:,}")
    lines.append(f"- **Total Rings**: {summary.total_rings:,}")
    lines.append("")

    # Ring Statistics
    lines.append("## Ring Statistics")
    if summary.ring_stats:
        max_value = max(summary.ring_stats.values()) if summary.ring_stats else 0
        if max_value > 1000:
            lines.append("Ring distribution by class:")
            lines.append("")
            for ring_class, count in sorted(summary.ring_stats.items(), key=lambda x: -x[1]):
                if count > 0:
                    lines.append(f"- **{ring_class}**: {int(count):,} rings")
        else:
            lines.append("Median densities for ring types (g/cm³):")
            lines.append("")
            for ring_type, median in sorted(summary.ring_stats.items()):
                if median > 0:
                    lines.append(f"- **{ring_type}**: {median:.2e}")
    else:
        lines.append("*Ring baseline not yet calculated for this sector.*")
    lines.append("")

    # Earth-like Worlds
    lines.append("## Earth-like Worlds (ELWs)")
    lines.append(f"- **Total ELWs**: {summary.total_elws}")
    lines.append(f"- **Systems with ELWs**: {summary.elw_systems}")
    lines.append("")

    if summary.hot_elw_patterns:
        lines.append("### Hot ELW Subsector Patterns")
        lines.append("Top subsector patterns (subsector + band when available) by ELW concentration:")
        lines.append("")
        lines.append("| Pattern | Systems | Total ELWs |")
        lines.append("|---------|---------|------------|")
        for pattern, sys_count, elw_count in summary.hot_elw_patterns:
            lines.append(f"| {pattern} | {sys_count} | {elw_count} |")
        lines.append("")

    # Bio-signatures
    lines.append("## Exobiology Hotspots")
    lines.append(f"- **Systems with bio-signatures**: {summary.total_bio_systems}")
    lines.append(f"- **High-value systems (5+ signatures)**: {summary.high_bio_systems}")
    lines.append("")

    if summary.hot_bio_patterns:
        lines.append("### Hot Bio-signature Subsector Patterns")
        lines.append("Top subsector patterns (subsector + band when available) with high bio-signature counts:")
        lines.append("")
        lines.append("| Pattern | Systems | Total Signatures |")
        lines.append("|---------|---------|------------------|")
        for pattern, sys_count, sig_count in summary.hot_bio_patterns:
            lines.append(f"| {pattern} | {sys_count} | {sig_count} |")
        lines.append("")

    # Exploration Recommendations
    lines.append("## Exploration Recommendations")
    lines.append("")

    if summary.hot_elw_patterns:
        top_elw = summary.hot_elw_patterns[0]
        sector, subsector, tail = _split_pattern(top_elw[0])
        if sector and subsector:
            lines.append("### Priority ELW Search")
            lines.append(f"- **Sector**: {sector}")
            lines.append(f"- **Subsector**: {subsector}")
            if tail:
                lines.append(f"- **Band/Tail**: {tail}")
            lines.append(f"- **Expected ELWs**: {top_elw[2]} across {top_elw[1]} systems")
            lines.append("")

    if summary.hot_bio_patterns:
        top_bio = summary.hot_bio_patterns[0]
        sector, subsector, tail = _split_pattern(top_bio[0])
        if sector and subsector:
            lines.append("### Priority Exobiology Search")
            lines.append(f"- **Sector**: {sector}")
            lines.append(f"- **Subsector**: {subsector}")
            if tail:
                lines.append(f"- **Band/Tail**: {tail}")
            lines.append(f"- **Expected Bio-sigs**: {top_bio[2]} across {top_bio[1]} systems")
            lines.append("")

    lines.append("---")
    lines.append(
        "\n*Use these subsector/mass-code patterns as a starting point for a "
        "spatial gap search (Mode: Spatial) centered on a system in the "
        "highest-concentration pattern above.*"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _count_systems(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) FROM systems").fetchone()
        return int(row[0] if row else 0)
    except sqlite3.Error:
        return 0


def _count_bodies(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) FROM bodies").fetchone()
        return int(row[0] if row else 0)
    except sqlite3.Error:
        return 0


def _count_rings(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) FROM rings").fetchone()
        return int(row[0] if row else 0)
    except sqlite3.Error:
        return 0


_RING_TYPE_ALIASES: dict[str, str] = {
    "icy": "Icy",
    "metal rich": "Metal Rich",
    "metalrich": "Metal Rich",
    "metallic": "Metallic",
    "metalic": "Metallic",
    "rocky": "Rocky",
}


def _normalize_ring_type(raw: str) -> Optional[str]:
    """Normalise a raw ring type string (EDSM/journal/plain) to a canonical key."""
    cleaned = raw.strip().lower().replace("_", " ").replace("-", " ")
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.replace("eringclass ", "").replace("eringclass", "")
    return _RING_TYPE_ALIASES.get(cleaned) or _RING_TYPE_ALIASES.get(cleaned.replace(" ", ""))


def _calculate_ring_stats(conn: sqlite3.Connection) -> dict[str, float]:
    """Median surface density per canonical ring type (Icy, Metal Rich,
    Metallic, Rocky), computed from the rings table's raw_json."""
    stats: dict[str, float] = {}

    try:
        densities: dict[str, list[float]] = defaultdict(list)
        for (ring_class_col, raw_json_col) in conn.execute(
            "SELECT ring_class, raw_json FROM rings WHERE raw_json IS NOT NULL"
        ):
            try:
                d = json.loads(raw_json_col)
            except (ValueError, TypeError):
                continue

            raw_type = ring_class_col or d.get("type") or ""
            ring_type = _normalize_ring_type(str(raw_type))
            if not ring_type:
                continue

            try:
                mass = float(d.get("mass") or 0)
                inner = float(d.get("innerRadius") or 0)
                outer = float(d.get("outerRadius") or 0)
            except (ValueError, TypeError):
                continue

            if mass <= 0 or outer <= inner or inner <= 0:
                continue

            area = math.pi * (outer ** 2 - inner ** 2)
            if area > 0:
                densities[ring_type].append(mass / area)

        for ring_type, vals in densities.items():
            if vals:
                stats[ring_type] = statistics.median(vals)

    except sqlite3.Error:
        pass

    return stats


def _analyze_elws(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Analyze ELW distribution and return (system_name, elw_count) pairs."""
    try:
        query = """
            SELECT system_name, COUNT(*) as elw_count
            FROM bodies
            WHERE sub_type = 'Earth-like world'
            GROUP BY system_name
            ORDER BY elw_count DESC, system_name
        """
        return list(conn.execute(query).fetchall())
    except sqlite3.Error:
        return []


def _analyze_biosigs(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Analyze bio-signature distribution and return (system_name, sig_count) pairs."""
    try:
        query = """
            SELECT system_name, raw_json
            FROM bodies
            WHERE raw_json LIKE '%signals%'
        """
        system_biosigs: dict[str, int] = {}

        for system_name, raw_json in conn.execute(query):
            try:
                data = json.loads(raw_json)
                if "signals" in data and isinstance(data["signals"], dict):
                    signals_data = data["signals"]
                    if "signals" in signals_data and isinstance(signals_data["signals"], dict):
                        bio_count = sum(
                            v for k, v in signals_data["signals"].items()
                            if "Biological" in k
                        )
                        if bio_count > 0:
                            system_biosigs[system_name] = system_biosigs.get(system_name, 0) + bio_count
            except (json.JSONDecodeError, TypeError):
                continue

        results = [(sys, count) for sys, count in system_biosigs.items()]
        results.sort(key=lambda x: (-x[1], x[0]))
        return results

    except sqlite3.Error:
        return []


def _group_by_pattern(
    systems_data: list[tuple[str, int]],
    top_k: int = 10,
    min_value: int = 1
) -> list[tuple[str, int, int]]:
    """Group systems by sub-sector pattern and return top patterns."""
    pattern_data: dict[str, list[int]] = defaultdict(list)

    for system_name, count in systems_data:
        if count < min_value:
            continue
        pattern = _parse_system_pattern(system_name)
        if pattern:
            pattern_data[pattern].append(count)

    results = []
    for pattern, counts in pattern_data.items():
        system_count = len(counts)
        total_value = sum(counts)
        results.append((pattern, system_count, total_value))

    results.sort(key=lambda x: (-x[2], -x[1], x[0]))
    return results[:top_k]


def _parse_system_pattern(system_name: str) -> Optional[str]:
    """Parse system name to extract sector + subsector + optional band/tail.

    Examples:
        "Crooma AB-C d1-123" -> "Crooma AB-C d1"
        "Phraa Blao AA-A g0" -> "Phraa Blao AA-A g0"
    """
    tokens = system_name.split()
    if len(tokens) < 2:
        return None

    subsector_idx = None
    for idx, token in enumerate(tokens):
        if re.match(r"^[A-Z]{2}-[A-Z]$", token, re.IGNORECASE):
            subsector_idx = idx
            break

    if subsector_idx is None or subsector_idx == 0:
        return None

    sector = " ".join(tokens[:subsector_idx])
    subsector = tokens[subsector_idx]
    tail = tokens[-1] if len(tokens) > subsector_idx + 1 else None

    if tail:
        match = re.match(r"^([a-z]\d+)-\d+$", tail, re.IGNORECASE)
        if match:
            mass_band = match.group(1).lower()
            return f"{sector} {subsector} {mass_band}"
        if re.match(r"^[a-z]\d+$", tail, re.IGNORECASE):
            return f"{sector} {subsector} {tail.lower()}"

    return f"{sector} {subsector}"


def _split_pattern(pattern: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Split a pattern into sector, subsector, and optional tail."""
    tokens = pattern.split()
    if len(tokens) < 2:
        return None, None, None

    subsector_idx = None
    for idx, token in enumerate(tokens):
        if re.match(r"^[A-Z]{2}-[A-Z]$", token, re.IGNORECASE):
            subsector_idx = idx
            break

    if subsector_idx is None or subsector_idx == 0:
        return None, None, None

    sector = " ".join(tokens[:subsector_idx])
    subsector = tokens[subsector_idx]
    tail = tokens[subsector_idx + 1] if len(tokens) > subsector_idx + 1 else None
    return sector, subsector, tail


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a sector baseline summary report from a sector library SQLite database."
    )
    parser.add_argument("--db", type=Path, required=True, help="Path to sector library SQLite file")
    parser.add_argument("--sector", required=True, help="Sector name (used in the report title/output filename)")
    parser.add_argument("--out", type=Path, default=None,
                         help="Output .md path (default: alongside --db, as sector_<slug>_summary.md)")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        return 1

    summary = analyze_sector(str(args.db), args.sector)
    if summary is None:
        print(f"ERROR: analysis failed for sector {args.sector!r}", file=sys.stderr)
        return 1

    output_path = args.out or (args.db.parent / f"sector_{sanitize_prefix(args.sector)}_summary.md")
    generate_summary_report(summary, output_path)
    print(f"Summary written to: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
sector_survey.py — High-value system survey for a sector SQLite DB.

Identifies high-value systems across five categories:
  - Earth-like Worlds (ELW)
  - Water Worlds (WW)
  - Ammonia Worlds (AW)
  - Top Icy rings by surface density
  - Top Metallic rings by surface density
  - Top systems by biosignature count

Then queries Spansh to determine claimed/unclaimed status, and performs
spatial cluster analysis (any cluster of 2+ high-value systems within 100 ly
of a common centroid).

Usage:
    python scripts/sector_survey.py --sector-db data/sector_library/sector_WREGOE.sqlite
    python scripts/sector_survey.py --sector-db data/sector_library/sector_WREGOE.sqlite \\
        --elw-min 2 --ww-min 3 --aw-min 2 --top-rings 10 --top-bio 15 \\
        --cluster-radius 75 --output out/wregoe_survey.csv --no-spansh
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False


# ── Constants ─────────────────────────────────────────────────────────────────

GALACTIC_MEDIANS = {
    "Icy":        8.591068e-06,
    "Metallic":   9.251326e-06,
    "Metal Rich": 8.998799e-06,
    "Rocky":      8.975781e-06,
}

SPANSH_SEARCH_URL = "https://spansh.co.uk/api/systems/search"
SPANSH_BATCH_SIZE = 50
SPANSH_DELAY_S    = 1.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def ring_density(mass: float, inner_m: float, outer_m: float) -> float:
    if mass <= 0 or outer_m <= inner_m or inner_m <= 0:
        return 0.0
    area = math.pi * (outer_m ** 2 - inner_m ** 2)
    return mass / area if area > 0 else 0.0


def distance_3d(a: tuple, b: tuple) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def sigma(density: float, ring_class: str) -> Optional[float]:
    median = GALACTIC_MEDIANS.get(ring_class)
    if median and density > 0:
        return density / median
    return None


def resolve_db_path(arg: str) -> Path:
    p = Path(arg)
    if p.exists():
        return p
    # Try common sector_library location
    alt = Path("data") / "sector_library" / arg
    if alt.exists():
        return alt
    raise FileNotFoundError(f"Sector DB not found: {arg}")


# ── Data extraction ───────────────────────────────────────────────────────────

def extract_high_value_systems(db_path: Path, args) -> dict:
    """
    Returns a dict with keys: elws, wws, aws, icy_rings, metallic_rings, bio,
    each containing a list of dicts with system_name, coords, and category metadata.
    Also returns 'systems_coords': {system_name: (x, y, z)}.
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))

    # Check schema
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "systems" not in tables or "bodies" not in tables:
        conn.close()
        raise ValueError(f"DB at {db_path} is missing expected tables (systems, bodies).")

    has_rings = "rings" in tables

    # ── Coordinates cache ────────────────────────────────────────────────────
    coords_map: dict[str, tuple] = {}
    for name, x, y, z in conn.execute("SELECT name, x, y, z FROM systems"):
        if name and x is not None:
            coords_map[name] = (float(x), float(y), float(z))

    def get_coords(system_name: str) -> Optional[tuple]:
        return coords_map.get(system_name)

    # ── ELWs ─────────────────────────────────────────────────────────────────
    elw_min = args.elw_min
    elw_rows = conn.execute("""
        SELECT system_name, COUNT(*) as cnt
        FROM bodies WHERE sub_type = 'Earth-like world'
        GROUP BY system_key HAVING cnt >= ?
        ORDER BY cnt DESC, system_name
    """, (elw_min,)).fetchall()
    elws = [
        {"system_name": r[0], "elw_count": r[1], "coords": get_coords(r[0])}
        for r in elw_rows
    ]

    # ── Water Worlds ─────────────────────────────────────────────────────────
    ww_min = args.ww_min
    ww_rows = conn.execute("""
        SELECT system_name, COUNT(*) as cnt
        FROM bodies WHERE sub_type = 'Water world'
        GROUP BY system_key HAVING cnt >= ?
        ORDER BY cnt DESC, system_name
    """, (ww_min,)).fetchall()
    wws = [
        {"system_name": r[0], "ww_count": r[1], "coords": get_coords(r[0])}
        for r in ww_rows
    ]

    # ── Ammonia Worlds ───────────────────────────────────────────────────────
    aw_min = args.aw_min
    aw_rows = conn.execute("""
        SELECT system_name, COUNT(*) as cnt
        FROM bodies WHERE sub_type = 'Ammonia world'
        GROUP BY system_key HAVING cnt >= ?
        ORDER BY cnt DESC, system_name
    """, (aw_min,)).fetchall()
    aws = [
        {"system_name": r[0], "aw_count": r[1], "coords": get_coords(r[0])}
        for r in aw_rows
    ]

    # ── Rings ────────────────────────────────────────────────────────────────
    icy_rings:     list[dict] = []
    metallic_rings: list[dict] = []

    if has_rings:
        for ring_name, rj, system_name in conn.execute(
            "SELECT ring_name, raw_json, system_name FROM rings WHERE raw_json IS NOT NULL"
        ):
            try:
                d = json.loads(rj)
                rtype = (d.get("type") or "").strip()
                rtype_low = rtype.lower()
                mass  = float(d.get("mass") or 0)
                inner = float(d.get("innerRadius") or 0)
                outer = float(d.get("outerRadius") or 0)
                dens  = ring_density(mass, inner, outer)
                if dens <= 0:
                    continue
                entry = {
                    "system_name": system_name,
                    "ring_name":   ring_name,
                    "ring_type":   rtype,
                    "density":     dens,
                    "coords":      get_coords(system_name),
                }
                if "icy" in rtype_low:
                    entry["sigma_vs_galactic"] = sigma(dens, "Icy")
                    icy_rings.append(entry)
                elif "metallic" in rtype_low and "metal rich" not in rtype_low:
                    entry["sigma_vs_galactic"] = sigma(dens, "Metallic")
                    metallic_rings.append(entry)
            except Exception:
                continue

    icy_rings.sort(key=lambda x: -x["density"])
    metallic_rings.sort(key=lambda x: -x["density"])
    top_n = args.top_rings
    icy_rings     = icy_rings[:top_n]
    metallic_rings = metallic_rings[:top_n]

    # ── Biosignatures ────────────────────────────────────────────────────────
    system_bio: dict[str, int] = defaultdict(int)
    for system_name, rj in conn.execute(
        "SELECT system_name, raw_json FROM bodies WHERE raw_json LIKE '%Biological%'"
    ):
        try:
            d = json.loads(rj)
            bio = d.get("signals", {}).get("signals", {}).get(
                "$SAA_SignalType_Biological;", 0
            )
            if bio:
                system_bio[system_name] += int(bio)
        except Exception:
            continue

    top_bio_pairs = sorted(system_bio.items(), key=lambda x: -x[1])[: args.top_bio]
    bio_list = [
        {"system_name": name, "bio_count": count, "coords": get_coords(name)}
        for name, count in top_bio_pairs
    ]

    # ── Body-type presence map (any count, for combo detection) ─────────────
    # Maps system_name -> set of categories present (regardless of threshold).
    body_presence: dict[str, set] = defaultdict(set)
    for system_name, sub_type in conn.execute(
        "SELECT system_name, sub_type FROM bodies "
        "WHERE sub_type IN ('Earth-like world','Water world','Ammonia world')"
    ):
        label_map = {
            "Earth-like world": "ELW",
            "Water world":      "WW",
            "Ammonia world":    "AW",
        }
        body_presence[system_name].add(label_map[sub_type])
    # Bio presence: any system with bio signals
    for name in system_bio:
        if system_bio[name] > 0:
            body_presence[name].add("bio")
    # Ring presence: any system with icy or metallic rings (full ring list, pre-truncation)
    if has_rings:
        for ring_name, rj, system_name in conn.execute(
            "SELECT ring_name, raw_json, system_name FROM rings WHERE raw_json IS NOT NULL"
        ):
            try:
                d = json.loads(rj)
                rtype = (d.get("type") or "").lower()
                if "icy" in rtype:
                    body_presence[system_name].add("icy_ring")
                elif "metallic" in rtype and "metal rich" not in rtype:
                    body_presence[system_name].add("metallic")
            except Exception:
                continue

    # ── Cluster secondary pool: single ELW/WW/AW below the main thresholds ──
    # These can join a cluster but cannot anchor one.
    secondary_rows = conn.execute("""
        SELECT system_name, sub_type, COUNT(*) as cnt
        FROM bodies
        WHERE sub_type IN ('Earth-like world', 'Water world', 'Ammonia world')
        GROUP BY system_key, sub_type
    """).fetchall()
    secondary: list[dict] = []
    for system_name, sub_type, cnt in secondary_rows:
        cat_map = {
            "Earth-like world": ("ELW_single", args.elw_min),
            "Water world":      ("WW_single",  args.ww_min),
            "Ammonia world":    ("AW_single",  args.aw_min),
        }
        cat_label, threshold = cat_map[sub_type]
        if cnt < threshold:  # only those below the high-value threshold
            secondary.append({
                "system_name": system_name,
                "categories":  [cat_label],
                "coords":      get_coords(system_name),
            })

    conn.close()

    return {
        "elws":              elws,
        "wws":               wws,
        "aws":               aws,
        "icy_rings":         icy_rings,
        "metallic_rings":    metallic_rings,
        "bio":               bio_list,
        "coords_map":        coords_map,
        "cluster_secondary": secondary,
        "body_presence":     body_presence,
    }


# ── Cluster analysis ──────────────────────────────────────────────────────────

def find_clusters(
    anchor_systems: list[dict],
    all_candidates: list[dict],
    radius_ly: float,
) -> list[dict]:
    """
    Identify clusters of 2+ systems within radius_ly of a common centroid,
    where each cluster must contain at least one anchor (high-value) system.

    anchor_systems: high-value systems that can seed/anchor a cluster.
    all_candidates: anchor_systems + secondary (single ELW/WW/AW) systems.
                    All are eligible cluster members once a cluster is anchored.

    Returns list of cluster dicts with keys: centroid, members, radius_max,
    anchor_count.
    """
    candidates = [s for s in all_candidates if s.get("coords")]
    if len(candidates) < 2:
        return []

    anchor_names = {s["system_name"] for s in anchor_systems if s.get("coords")}
    n = len(candidates)

    # Index lookup for quick anchor check
    def is_anchor(idx: int) -> bool:
        return candidates[idx]["system_name"] in anchor_names

    # Build adjacency within 2×radius (loose pre-filter)
    neighbours: dict[int, set[int]] = defaultdict(set)
    for i in range(n):
        for j in range(i + 1, n):
            d = distance_3d(candidates[i]["coords"], candidates[j]["coords"])
            if d <= radius_ly * 2:
                neighbours[i].add(j)
                neighbours[j].add(i)

    # Only seed clusters from anchor systems
    visited: set[int] = set()
    clusters = []

    for seed_idx in range(n):
        if not is_anchor(seed_idx):
            continue
        if seed_idx in visited:
            continue

        group = {seed_idx} | neighbours[seed_idx]

        # Iteratively refine centroid
        for _ in range(10):
            coords_in_group = [candidates[i]["coords"] for i in group]
            cx = statistics.mean(c[0] for c in coords_in_group)
            cy = statistics.mean(c[1] for c in coords_in_group)
            cz = statistics.mean(c[2] for c in coords_in_group)
            centroid = (cx, cy, cz)
            new_group = {
                i for i in range(n)
                if distance_3d(candidates[i]["coords"], centroid) <= radius_ly
            }
            if new_group == group:
                break
            group = new_group

        if len(group) < 2:
            continue

        # Must contain at least one anchor
        if not any(is_anchor(i) for i in group):
            continue

        member_key = frozenset(group)
        if any(c["_member_key"] == member_key for c in clusters):
            continue

        coords_in_group = [candidates[i]["coords"] for i in group]
        cx = statistics.mean(c[0] for c in coords_in_group)
        cy = statistics.mean(c[1] for c in coords_in_group)
        cz = statistics.mean(c[2] for c in coords_in_group)
        centroid = (cx, cy, cz)
        max_r = max(distance_3d(candidates[i]["coords"], centroid) for i in group)
        anchor_count = sum(1 for i in group if is_anchor(i))

        clusters.append({
            "_member_key":  member_key,
            "centroid":     (round(cx, 2), round(cy, 2), round(cz, 2)),
            "radius_max":   round(max_r, 2),
            "member_count": len(group),
            "anchor_count": anchor_count,
            "members":      [candidates[i] for i in sorted(group)],
        })
        visited.add(seed_idx)

    # Sort by anchor count desc, then total members desc
    clusters.sort(key=lambda c: (-c["anchor_count"], -c["member_count"]))
    return clusters


# ── Spansh API ────────────────────────────────────────────────────────────────

async def _query_batch(session, names: list[str]) -> list[dict]:
    payload = {
        "filters": {"name": {"value": names, "comparison": "in"}},
        "size": SPANSH_BATCH_SIZE,
        "page": 0,
    }
    async with session.post(
        SPANSH_SEARCH_URL,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json()
        return data.get("results", [])


async def query_spansh(system_names: list[str]) -> dict[str, dict]:
    """
    Returns {system_name: spansh_record} for all found systems.
    Systems not returned by Spansh are implicitly unclaimed/unknown.
    """
    if not _HAS_AIOHTTP:
        print("WARNING: aiohttp not installed — skipping Spansh queries. "
              "Run: pip install aiohttp", file=sys.stderr)
        return {}

    results: dict[str, dict] = {}
    batches = [system_names[i: i + SPANSH_BATCH_SIZE]
               for i in range(0, len(system_names), SPANSH_BATCH_SIZE)]

    async with aiohttp.ClientSession() as session:
        for idx, batch in enumerate(batches, 1):
            print(f"  Spansh batch {idx}/{len(batches)} ({len(batch)} systems)…")
            try:
                records = await _query_batch(session, batch)
                for r in records:
                    name = r.get("name", "")
                    if name:
                        results[name] = r
            except Exception as exc:
                print(f"  WARNING: Spansh batch {idx} failed: {exc}", file=sys.stderr)
            if idx < len(batches):
                await asyncio.sleep(SPANSH_DELAY_S)

    return results


def spansh_claimed(record: Optional[dict]) -> str:
    """Return 'claimed', 'unclaimed', or 'unknown'."""
    if record is None:
        return "unknown"
    pop = record.get("population") or 0
    faction = record.get("controlling_minor_faction") or ""
    # A system is 'claimed' if Spansh has a record AND it has been visited
    # (updated_at present). Population > 0 means colonised.
    if pop and int(pop) > 0:
        return "colonised"
    if record.get("updated_at"):
        return "visited"
    return "unknown"


# ── Output ────────────────────────────────────────────────────────────────────

def write_csv(output_path: Path, data: dict, spansh_data: dict[str, dict]) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)

        def coords_str(coords):
            if not coords:
                return "", "", ""
            return round(coords[0], 2), round(coords[1], 2), round(coords[2], 2)

        def spansh_cols(system_name):
            rec = spansh_data.get(system_name)
            return (
                spansh_claimed(rec),
                (rec or {}).get("updated_at", ""),
                (rec or {}).get("population", ""),
                (rec or {}).get("controlling_minor_faction", ""),
            )

        # ELWs
        w.writerow(["SECTION", f"Earth-like Worlds (>={data['_args'].elw_min} per system)"])
        w.writerow(["system_name", "elw_count", "x", "y", "z",
                    "spansh_status", "spansh_updated", "population", "controlling_faction"])
        for s in data["elws"]:
            x, y, z = coords_str(s["coords"])
            w.writerow([s["system_name"], s["elw_count"], x, y, z, *spansh_cols(s["system_name"])])
        w.writerow([])

        # Water Worlds
        w.writerow(["SECTION", f"Water Worlds (>={data['_args'].ww_min} per system)"])
        w.writerow(["system_name", "ww_count", "x", "y", "z",
                    "spansh_status", "spansh_updated", "population", "controlling_faction"])
        for s in data["wws"]:
            x, y, z = coords_str(s["coords"])
            w.writerow([s["system_name"], s["ww_count"], x, y, z, *spansh_cols(s["system_name"])])
        w.writerow([])

        # Ammonia Worlds
        w.writerow(["SECTION", f"Ammonia Worlds (>={data['_args'].aw_min} per system)"])
        w.writerow(["system_name", "aw_count", "x", "y", "z",
                    "spansh_status", "spansh_updated", "population", "controlling_faction"])
        for s in data["aws"]:
            x, y, z = coords_str(s["coords"])
            w.writerow([s["system_name"], s["aw_count"], x, y, z, *spansh_cols(s["system_name"])])
        w.writerow([])

        # Top Bio
        w.writerow(["SECTION", f"Top {data['_args'].top_bio} Systems by Biosignature Count"])
        w.writerow(["rank", "system_name", "bio_count", "x", "y", "z",
                    "spansh_status", "spansh_updated", "population", "controlling_faction"])
        for i, s in enumerate(data["bio"], 1):
            x, y, z = coords_str(s["coords"])
            w.writerow([i, s["system_name"], s["bio_count"], x, y, z,
                        *spansh_cols(s["system_name"])])
        w.writerow([])

        # Icy Rings
        w.writerow(["SECTION", f"Top {data['_args'].top_rings} Icy Rings by Surface Density"])
        w.writerow(["rank", "system_name", "ring_name", "surface_density",
                    "sigma_vs_galactic", "x", "y", "z",
                    "spansh_status", "spansh_updated"])
        for i, s in enumerate(data["icy_rings"], 1):
            x, y, z = coords_str(s["coords"])
            sig = f"{s['sigma_vs_galactic']:.2f}" if s.get("sigma_vs_galactic") else ""
            sc = spansh_cols(s["system_name"])
            w.writerow([i, s["system_name"], s["ring_name"],
                        s["density"], sig, x, y, z, sc[0], sc[1]])
        w.writerow([])

        # Metallic Rings
        w.writerow(["SECTION", f"Top {data['_args'].top_rings} Metallic Rings by Surface Density"])
        w.writerow(["rank", "system_name", "ring_name", "surface_density",
                    "sigma_vs_galactic", "x", "y", "z",
                    "spansh_status", "spansh_updated"])
        for i, s in enumerate(data["metallic_rings"], 1):
            x, y, z = coords_str(s["coords"])
            sig = f"{s['sigma_vs_galactic']:.2f}" if s.get("sigma_vs_galactic") else ""
            sc = spansh_cols(s["system_name"])
            w.writerow([i, s["system_name"], s["ring_name"],
                        s["density"], sig, x, y, z, sc[0], sc[1]])
        w.writerow([])

        # High-Value Combination Systems
        w.writerow(["SECTION", "High-Value Combination Systems (2+ categories in same system)"])
        w.writerow(["system_name", "categories", "x", "y", "z",
                    "spansh_status", "spansh_updated", "population", "controlling_faction"])
        for s in data["combos"]:
            x, y, z = coords_str(s["coords"])
            w.writerow([s["system_name"], "|".join(s["categories"]),
                        x, y, z, *spansh_cols(s["system_name"])])
        w.writerow([])

        # Clusters
        w.writerow(["SECTION",
                    f"Spatial Clusters (anchored by high-value system, within {data['_args'].cluster_radius} ly)"])
        w.writerow(["cluster_id", "anchor_count", "member_count",
                    "centroid_x", "centroid_y", "centroid_z",
                    "radius_max_ly", "system_name", "categories", "is_anchor", "x", "y", "z"])
        anchor_names = {s["system_name"] for s in data["tagged"]}
        for cid, cluster in enumerate(data["clusters"], 1):
            cx, cy, cz = cluster["centroid"]
            for m in cluster["members"]:
                x, y, z = coords_str(m["coords"])
                is_anc = "Y" if m["system_name"] in anchor_names else "N"
                w.writerow([cid, cluster["anchor_count"], cluster["member_count"],
                             cx, cy, cz, cluster["radius_max"],
                             m["system_name"], "|".join(m.get("categories", [])),
                             is_anc, x, y, z])
        w.writerow([])


def print_summary(data: dict) -> None:
    args = data["_args"]
    print("\n" + "=" * 70)
    print(f"  SECTOR SURVEY SUMMARY")
    print("=" * 70)
    print(f"  ELWs (>={args.elw_min}/system)       : {len(data['elws'])} systems")
    print(f"  Water Worlds (>={args.ww_min}/system) : {len(data['wws'])} systems")
    print(f"  Ammonia Worlds (>={args.aw_min}/system): {len(data['aws'])} systems")
    print(f"  Top {args.top_bio} bio systems   : up to {args.top_bio}")
    print(f"  Top {args.top_rings} icy rings    : {len(data['icy_rings'])} found")
    print(f"  Top {args.top_rings} metallic rings: {len(data['metallic_rings'])} found")
    print(f"  Combo systems (2+ categories): {len(data['combos'])}")
    print(f"  Clusters (<={args.cluster_radius} ly radius): {len(data['clusters'])} found")

    if data["elws"]:
        best = data["elws"][0]
        print(f"\n  Best ELW system  : {best['system_name']} ({best['elw_count']} ELWs)")
    if data["wws"]:
        best = data["wws"][0]
        print(f"  Best WW system   : {best['system_name']} ({best['ww_count']} WWs)")
    if data["aws"]:
        best = data["aws"][0]
        print(f"  Best AW system   : {best['system_name']} ({best['aw_count']} AWs)")
    if data["bio"]:
        best = data["bio"][0]
        print(f"  Most bio sigs    : {best['system_name']} ({best['bio_count']} signals)")
    if data["icy_rings"]:
        best = data["icy_rings"][0]
        sig = f"  s={best['sigma_vs_galactic']:.1f}x" if best.get("sigma_vs_galactic") else ""
        print(f"  Best icy ring    : {best['ring_name']} ({best['density']:.3e}{sig})")
    if data["metallic_rings"]:
        best = data["metallic_rings"][0]
        sig = f"  s={best['sigma_vs_galactic']:.1f}x" if best.get("sigma_vs_galactic") else ""
        print(f"  Best metallic ring: {best['ring_name']} ({best['density']:.3e}{sig})")

    if data["combos"]:
        best = data["combos"][0]
        print(f"  Best combo system: {best['system_name']} [{', '.join(best['categories'])}]")

    if data["clusters"]:
        top = data["clusters"][0]
        print(f"\n  Best cluster     : {top['anchor_count']} anchor + "
              f"{top['member_count'] - top['anchor_count']} secondary = "
              f"{top['member_count']} systems within {top['radius_max']} ly")
        print(f"    Centroid: {top['centroid']}")
        anchor_names = {s["system_name"] for s in data["tagged"]}
        for m in top["members"]:
            marker = "[ANCHOR]" if m["system_name"] in anchor_names else "[+]"
            print(f"    {marker} {m['system_name']}  [{', '.join(m.get('categories', []))}]")

    print("=" * 70)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Survey a sector SQLite DB for high-value systems."
    )
    parser.add_argument("--sector-db", required=True,
                        help="Path to sector_*.sqlite (or filename in data/sector_library/)")
    parser.add_argument("--elw-min",       type=int,   default=2,
                        help="Min ELWs per system (default: 2)")
    parser.add_argument("--ww-min",        type=int,   default=3,
                        help="Min Water Worlds per system (default: 3)")
    parser.add_argument("--aw-min",        type=int,   default=2,
                        help="Min Ammonia Worlds per system (default: 2)")
    parser.add_argument("--top-rings",     type=int,   default=5,
                        help="Top N rings per class (default: 5)")
    parser.add_argument("--top-bio",       type=int,   default=10,
                        help="Top N bio systems (default: 10)")
    parser.add_argument("--cluster-radius",type=float, default=75.0,
                        help="Cluster search radius in ly (default: 75)")
    parser.add_argument("--output",        default=None,
                        help="Output CSV path (default: auto-named in data/sector_library/)")
    parser.add_argument("--no-spansh",     action="store_true",
                        help="Skip Spansh API queries")
    args = parser.parse_args(argv)

    # Resolve DB path
    try:
        db_path = resolve_db_path(args.sector_db)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    sector_label = db_path.stem.replace("sector_", "").replace("_", " ").title()
    print(f"Sector DB : {db_path}")
    print(f"Sector    : {sector_label}")

    # Extract
    print("\nExtracting high-value systems…")
    data = extract_high_value_systems(db_path, args)
    data["_args"] = args

    # Build unified tagged list (high-value anchors)
    tagged: list[dict] = []
    seen: dict[str, dict] = {}

    def tag(system_name, category, coords):
        if system_name not in seen:
            seen[system_name] = {"system_name": system_name, "coords": coords, "categories": []}
            tagged.append(seen[system_name])
        seen[system_name]["categories"].append(category)

    for s in data["elws"]:           tag(s["system_name"], "ELW",      s["coords"])
    for s in data["wws"]:            tag(s["system_name"], "WW",       s["coords"])
    for s in data["aws"]:            tag(s["system_name"], "AW",       s["coords"])
    for s in data["bio"]:            tag(s["system_name"], "bio",      s["coords"])
    for s in data["icy_rings"]:      tag(s["system_name"], "icy_ring", s["coords"])
    for s in data["metallic_rings"]: tag(s["system_name"], "metallic", s["coords"])

    # Combo systems: anchor system that meets one high-value threshold AND
    # has at least one body/signal of a different category (even if below threshold).
    body_presence = data["body_presence"]
    combos = []
    for s in tagged:
        # Categories already qualifying this system as a high-value anchor
        anchor_cats = set(s["categories"])
        # All categories present in this system (any count)
        all_present = body_presence.get(s["system_name"], set())
        # Combine: anchor cats + any other category present
        combined = anchor_cats | all_present
        if len(combined) >= 2:
            combos.append({
                "system_name": s["system_name"],
                "coords":      s["coords"],
                # Show high-value anchor categories first, then additional presence
                "categories":  sorted(anchor_cats) + sorted(all_present - anchor_cats),
            })
    combos.sort(key=lambda s: (-len(s["categories"]), s["system_name"]))
    data["combos"] = combos
    data["tagged"] = tagged  # needed by write_csv for is_anchor lookup

    print(f"Unique high-value systems : {len(tagged)}")
    print(f"Combo systems (2+ categories): {len(combos)}")

    # Cluster analysis: anchors = tagged; pool = tagged + secondary singles
    secondary = data["cluster_secondary"]
    # Deduplicate secondary against already-tagged systems
    tagged_names = {s["system_name"] for s in tagged}
    secondary_deduped = [s for s in secondary if s["system_name"] not in tagged_names]
    all_candidates = tagged + secondary_deduped

    print(f"Running cluster analysis (radius={args.cluster_radius} ly, "
          f"pool={len(all_candidates)} systems, anchors={len(tagged)})...")
    data["clusters"] = find_clusters(tagged, all_candidates, args.cluster_radius)
    print(f"Clusters found: {len(data['clusters'])}")

    # Spansh — query anchors only (secondary singles not worth the API cost)
    all_system_names = [s["system_name"] for s in tagged]
    if args.no_spansh:
        spansh_data: dict[str, dict] = {}
        print("Spansh queries skipped (--no-spansh).")
    else:
        if not _HAS_AIOHTTP:
            print("WARNING: aiohttp not available — install with: pip install aiohttp")
            spansh_data = {}
        else:
            print(f"\nQuerying Spansh for {len(all_system_names)} systems…")
            spansh_data = asyncio.run(query_spansh(all_system_names))
            found    = len(spansh_data)
            unknown  = len(all_system_names) - found
            print(f"  Found in Spansh   : {found}")
            print(f"  Not in Spansh     : {unknown}  (likely undiscovered/unreported)")

    # Output path
    if args.output:
        output_path = Path(args.output)
    else:
        stem = db_path.stem.replace("sector_", "survey_")
        output_path = db_path.parent / f"{stem}.csv"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(output_path, data, spansh_data)
    print(f"\nCSV written: {output_path}")

    print_summary(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())

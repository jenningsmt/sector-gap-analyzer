#!/usr/bin/env python3
"""
gap_naming.py — Shared procedural system-name parsing and sort-key helpers.
=============================================================================
Single source of truth for the naming grammar used by gap_full_export.py,
gap_extrapolate_export.py, gap_spatial_export.py, and
aggregate_gap_master_list.py — avoids the same parsing/sort logic drifting
out of sync across those independently-runnable scripts.

Elite Dangerous procedural system names follow:
    <Sector> <SS-S> <mass code><boxel>-<serial>
e.g. "Heart Sector AA-Q b5-3":
    subsector = "AA-Q", mass code = "b", boxel = 5, serial = 3
"""

from __future__ import annotations

import re
from typing import Optional

_SUBSECTOR_RE = re.compile(r"^[A-Z]{1,3}-[A-Z]$", re.IGNORECASE)
_TAIL_RE = re.compile(r"^([a-z]\d+-)(\d+)$", re.IGNORECASE)
_MASS_PREFIX_RE = re.compile(r"^([a-zA-Z])(\d+)$")


def find_subsector_index(tokens: list[str]) -> Optional[int]:
    """Find the index of the subsector token (e.g. 'AA-Q') in a token list."""
    for idx, token in enumerate(tokens):
        if _SUBSECTOR_RE.match(token):
            return idx
    return None


def parse_sequence_name(name: str) -> Optional[tuple[str, str, int]]:
    """
    Parse a procedural system name into (family, prefix, number).

    Example: "Heart Sector AA-Q b5-1"
      -> family = "Heart Sector AA-Q"
         prefix = "b5-"
         number = 1

    Returns None if the name does not follow the sequenced pattern.
    """
    tokens = [t for t in name.split() if t]
    if len(tokens) < 3:
        return None

    subsector_idx = find_subsector_index(tokens)
    if subsector_idx is None or subsector_idx == 0:
        # Fallback: treat first two tokens as family
        family = " ".join(tokens[:2])
        last = tokens[-1]
        m = _TAIL_RE.match(last)
        if not m:
            return None
        return family, m.group(1).lower(), int(m.group(2))

    sector = " ".join(tokens[:subsector_idx])
    subsector = tokens[subsector_idx]
    last = tokens[-1]
    m = _TAIL_RE.match(last)
    if not m:
        return None
    family = f"{sector} {subsector}"
    return family, m.group(1).lower(), int(m.group(2))


def extract_subsector(family: str) -> str:
    """Pull the subsector token back out of a 'family' string (sector + subsector)."""
    tokens = [t for t in family.split() if t]
    idx = find_subsector_index(tokens)
    return tokens[idx] if idx is not None else ""


def split_mass_prefix(prefix: str) -> tuple[str, int]:
    """
    Split a combined mass-code+boxel prefix into its parts.

    "b5-" or "b5" -> ("b", 5). Falls back to (cleaned_prefix, 0) if the
    prefix doesn't match the expected <letter><digits> shape.
    """
    cleaned = prefix.rstrip("-")
    m = _MASS_PREFIX_RE.match(cleaned)
    if not m:
        return cleaned.lower(), 0
    return m.group(1).lower(), int(m.group(2))


def group_sort_key(name: str) -> tuple:
    """
    In-game grouping order for a single system name: subsector -> mass code
    -> boxel -> serial, with the full name as a final tiebreaker.

    "Heart Sector AA-Q b5-3" -> ("AA-Q", "b", 5, 3, "Heart Sector AA-Q b5-3")
    Falls back to (subsector, "", 0, 0, name) for non-sequence names.
    """
    parsed = parse_sequence_name(name)
    if parsed is None:
        tokens = [t for t in name.split() if t]
        idx = find_subsector_index(tokens)
        subsector = tokens[idx] if idx is not None else ""
        return (subsector, "", 0, 0, name)
    family, prefix, number = parsed
    subsector = extract_subsector(family)
    mass_code, boxel = split_mass_prefix(prefix)
    return (subsector, mass_code, boxel, number, name)

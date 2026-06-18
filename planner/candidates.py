"""Candidate generation stubs for the strategic planner."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Optional

from planner_strategic.models import Candidate, JobProgress


def generate_candidates_gap(
    working_db_path: str,
    sector: Optional[str],
    count: int = 25,
    progress_cb: Optional[callable] = None,
) -> list[Candidate]:
    rows = _load_seed_rows(working_db_path, mode="gap")
    seed_candidates = [_candidate_from_row(row) for row in rows]
    for candidate in seed_candidates:
        _tag_gap_hypothesis(candidate)

    systems = _load_working_systems(working_db_path)
    names = [row["system_name"] for row in systems]
    families = _build_families(names)
    sequences = _build_sequences(names)

    desired = max(count, 50, len(seed_candidates))
    candidates = list(seed_candidates)
    existing = {candidate.system_name for candidate in candidates}

    if sequences:
        generated = _generate_sequence_gap_candidates(sequences, desired, existing)
        existing.update([cand.system_name for cand in generated])
        ordered = generated + seed_candidates
        _emit(
            progress_cb,
            JobProgress(phase="generate", current=len(ordered), total=len(ordered), message="Generated"),
        )
        return ordered
    elif families:
        top_families = _top_families(families, top_k=10)
        per_family = int(math.ceil(desired / len(top_families)))
        for family_key in top_families:
            generated = _generate_family_candidates(
                family_key,
                families[family_key],
                per_family,
                existing,
                tags=["GAP", "HYPOTHESIS"],
                rationale_prefix="GAP FAMILY",
            )
            candidates.extend(generated)
            existing.update([cand.system_name for cand in generated])

    scored = _score_candidates(candidates, families)
    ordered = _apply_dispersion(scored)
    _emit(
        progress_cb,
        JobProgress(phase="generate", current=len(ordered), total=len(ordered), message="Generated"),
    )
    return ordered


def generate_candidates_spatial(
    working_db_path: str,
    center_system: Optional[str],
    radius_ly: Optional[float],
    progress_cb: Optional[callable] = None,
) -> list[Candidate]:
    systems = _load_working_systems(working_db_path)
    if not systems:
        raise ValueError("No neighborhood systems available for spatial planning")

    families = _build_families([row["system_name"] for row in systems])
    _emit(
        progress_cb,
        JobProgress(
            phase="generate",
            current=len(systems),
            total=len(systems),
            message="Neighborhood systems loaded",
        ),
    )
    top_families = _top_families(families, top_k=10)
    _emit(
        progress_cb,
        JobProgress(
            phase="generate",
            current=len(top_families),
            total=len(top_families),
            message="Dominant families",
        ),
    )
    desired = 50
    existing: set[str] = set()
    candidates: list[Candidate] = []
    per_family = int(math.ceil(desired / max(len(top_families), 1)))
    for family_key in top_families:
        generated = _generate_family_candidates(
            family_key,
            families[family_key],
            per_family,
            existing,
            tags=["SPATIAL", "HYPOTHESIS"],
            rationale_prefix="SPATIAL FAMILY",
        )
        candidates.extend(generated)
        existing.update([cand.system_name for cand in generated])

    scored = _score_candidates(candidates, families)
    ordered = _apply_dispersion(scored)
    _emit(
        progress_cb,
        JobProgress(
            phase="generate",
            current=len(ordered),
            total=len(ordered),
            message="Candidates generated",
        ),
    )
    _emit(progress_cb, JobProgress(phase="generate", current=len(ordered), total=len(ordered), message="Generated"))
    return ordered


def generate_candidates_hot_subsector(
    working_db_path: str,
    sector: Optional[str],
    sub_sector: Optional[str],
    mass_code: Optional[str],
    count: int = 25,
    progress_cb: Optional[callable] = None,
) -> list[Candidate]:
    rows = _load_seed_rows(working_db_path, mode="hot_subsector")
    seed_candidates = [_candidate_from_row(row) for row in rows]
    for candidate in seed_candidates:
        if "HOT_SUBSECTOR" not in candidate.tags:
            candidate.tags.append("HOT_SUBSECTOR")
        if "HYPOTHESIS" not in candidate.tags:
            candidate.tags.append("HYPOTHESIS")

    systems = _load_working_systems(working_db_path)
    names = [row["system_name"] for row in systems]
    families = _build_families(names)
    sequences = _build_sequences(names)

    desired = max(count, 50, len(seed_candidates))
    candidates = list(seed_candidates)
    existing = {candidate.system_name for candidate in candidates}

    if sequences:
        generated = _generate_sequence_gap_candidates(sequences, desired, existing)
        for cand in generated:
            cand.tags = ["HOT_SUBSECTOR", "HYPOTHESIS"]
            cand.rationale = f"HOT SUB-SECTOR: {sector} {sub_sector} {mass_code} | {cand.rationale}"
        existing.update([cand.system_name for cand in generated])
        ordered = generated + seed_candidates
        _emit(
            progress_cb,
            JobProgress(phase="generate", current=len(ordered), total=len(ordered), message="Generated"),
        )
        return ordered
    elif families:
        top_families = _top_families(families, top_k=10)
        per_family = int(math.ceil(desired / len(top_families)))
        for family_key in top_families:
            generated = _generate_family_candidates(
                family_key,
                families[family_key],
                per_family,
                existing,
                tags=["HOT_SUBSECTOR", "HYPOTHESIS"],
                rationale_prefix=f"HOT SUB-SECTOR: {sector} {sub_sector} {mass_code}",
            )
            candidates.extend(generated)
            existing.update([cand.system_name for cand in generated])

    scored = _score_candidates(candidates, families)
    ordered = _apply_dispersion(scored)
    _emit(
        progress_cb,
        JobProgress(phase="generate", current=len(ordered), total=len(ordered), message="Generated"),
    )
    return ordered


def _load_seed_rows(working_db_path: str, mode: str) -> list[tuple]:
    conn = sqlite3.connect(working_db_path)
    try:
        return list(
            conn.execute(
                """
                SELECT system_name, base_score, distance_ly, tags, rationale
                FROM strategic_seed
                WHERE mode = ?
                ORDER BY base_score DESC, system_name ASC
                """,
                (mode,),
            )
        )
    finally:
        conn.close()


def _load_working_systems(working_db_path: str) -> list[dict]:
    conn = sqlite3.connect(working_db_path)
    try:
        return [
            {"system_name": row[0], "x": row[1], "y": row[2], "z": row[3]}
            for row in conn.execute("SELECT system_name, x, y, z FROM working_systems")
        ]
    finally:
        conn.close()


def _candidate_from_row(row: tuple) -> Candidate:
    system_name, base_score, distance_ly, tags_blob, rationale = row
    system_name = (system_name or "").strip() or "UNKNOWN"
    tags = []
    if tags_blob:
        try:
            tags = json.loads(tags_blob)
        except json.JSONDecodeError:
            tags = [tag.strip() for tag in tags_blob.split(",") if tag.strip()]
    score = float(base_score) if base_score is not None else 0.5
    return _candidate_from_seed(system_name, score, distance_ly, tags, rationale or "STUB: SEED")


def _candidate_from_seed(
    system_name: str,
    score: float,
    distance_ly: Optional[float],
    tags: list[str],
    rationale: str,
) -> Candidate:
    system_name = (system_name or "").strip() or "UNKNOWN"
    confidence = _bucket_confidence(score)
    return Candidate(
        system_name=system_name,
        score=score,
        confidence=confidence,
        distance_ly=distance_ly,
        tags=tags,
        rationale=rationale,
    )


def _tag_gap_hypothesis(candidate: Candidate) -> None:
    if "GAP" not in candidate.tags:
        candidate.tags.append("GAP")
    if "HYPOTHESIS" not in candidate.tags:
        candidate.tags.append("HYPOTHESIS")
    if candidate.rationale:
        if "HYPOTHESIS" not in candidate.rationale.upper():
            candidate.rationale = f"{candidate.rationale} | HYPOTHESIS"
    else:
        candidate.rationale = "HYPOTHESIS"


def _clean_sector(sector: str) -> str:
    cleaned = " ".join(part for part in sector.strip().split() if part)
    return cleaned or "UNKNOWN"


def _gap_name(sector: str, index: int) -> str:
    seed = hashlib.sha256(sector.encode("utf-8")).hexdigest()
    offset = int(seed[:6], 16)
    letters = _letters_from_number(offset + index)
    suffix = (offset + index) % 10
    return f"{sector} {letters[0]}{letters[1]}-{letters[2]} h{suffix}"


def _letters_from_number(value: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    value = value % (26 * 26 * 26)
    first = value // (26 * 26)
    second = (value // 26) % 26
    third = value % 26
    return alphabet[first] + alphabet[second] + alphabet[third]


def _build_sequences(names: list[str]) -> dict[tuple[str, str], list[int]]:
    sequences: dict[tuple[str, str], list[int]] = defaultdict(list)
    for name in names:
        parsed = _parse_sequence_name(name)
        if parsed is None:
            continue
        family, prefix, number = parsed
        sequences[(family, prefix)].append(number)
    for key in list(sequences.keys()):
        sequences[key] = sorted(set(sequences[key]))
    return sequences


def _parse_sequence_name(name: str) -> Optional[tuple[str, str, int]]:
    tokens = [token for token in name.split() if token]
    if len(tokens) < 3:
        return None

    subsector_idx = _find_subsector_index(tokens)
    if subsector_idx is None or subsector_idx == 0:
        # Fallback for non-procedural names: keep legacy behavior.
        family = " ".join(tokens[:2])
        last = tokens[-1]
        match = re.match(r"^([a-z]\d+-)(\d+)$", last, re.IGNORECASE)
        if not match:
            return None
        prefix = match.group(1).lower()
        number = int(match.group(2))
        return family, prefix, number

    sector = " ".join(tokens[:subsector_idx])
    subsector = tokens[subsector_idx]
    last = tokens[-1]
    match = re.match(r"^([a-z]\d+-)(\d+)$", last, re.IGNORECASE)
    if not match:
        return None
    prefix = match.group(1).lower()
    number = int(match.group(2))
    family = f"{sector} {subsector}"
    return family, prefix, number


def _generate_sequence_gap_candidates(
    sequences: dict[tuple[str, str], list[int]],
    desired: int,
    existing: set[str],
) -> list[Candidate]:
    density = {key: len(nums) for key, nums in sequences.items()}
    sorted_keys = sorted(density.keys(), key=lambda key: (-density[key], key[0], key[1]))
    gap_lists: dict[tuple[str, str], list[int]] = {}
    for key in sorted_keys:
        numbers = sequences[key]
        gaps = _missing_numbers(numbers, desired)
        if gaps:
            gap_lists[key] = gaps

    candidates: list[Candidate] = []
    index = 0
    while len(candidates) < desired and gap_lists:
        for key in sorted_keys:
            if key not in gap_lists:
                continue
            if not gap_lists[key]:
                gap_lists.pop(key, None)
                continue
            family, prefix = key
            number = gap_lists[key].pop(0)
            name = f"{family} {prefix}{number}"
            if name in existing:
                continue
            rationale = f"GAP: missing {prefix}{number} in {family}"
            candidates.append(
                _candidate_from_seed(
                    name,
                    0.6,
                    None,
                    ["GAP", "HYPOTHESIS"],
                    rationale,
                )
            )
            existing.add(name)
            if len(candidates) >= desired:
                break
        index += 1
        if index > desired * 2:
            break
    return candidates


def _missing_numbers(numbers: list[int], desired: int) -> list[int]:
    if not numbers:
        return []
    minimum = min(numbers)
    maximum = max(numbers)
    missing = [n for n in range(minimum, maximum + 1) if n not in numbers]
    if len(missing) >= desired:
        return missing
    lower = minimum - 1
    upper = maximum + 1
    while len(missing) < desired:
        if lower > 0:
            missing.append(lower)
            lower -= 1
            if len(missing) >= desired:
                break
        missing.append(upper)
        upper += 1
    return missing


def _family_key(system_name: str) -> str:
    tokens = [token for token in system_name.split() if token]
    if not tokens:
        return "UNKNOWN"
    subsector_idx = _find_subsector_index(tokens)
    if subsector_idx is not None and subsector_idx > 0:
        return " ".join(tokens[: subsector_idx + 1])
    if len(tokens) >= 2:
        return " ".join(tokens[:2])
    return tokens[0]


def _find_subsector_index(tokens: list[str]) -> Optional[int]:
    for idx, token in enumerate(tokens):
        if re.match(r"^[A-Z]{1,3}-[A-Z]$", token, re.IGNORECASE):
            return idx
    return None


def _build_families(names: list[str]) -> dict[str, list[str]]:
    families: dict[str, list[str]] = defaultdict(list)
    for name in names:
        key = _family_key(name)
        remainder = name[len(key) :].strip()
        if remainder:
            families[key].append(remainder)
    return families


def _top_families(families: dict[str, list[str]], top_k: int) -> list[str]:
    counts = [(key, len(values)) for key, values in families.items()]
    counts.sort(key=lambda item: (-item[1], item[0]))
    return [item[0] for item in counts[:top_k]]


def _generate_family_candidates(
    family_key: str,
    remainders: list[str],
    count: int,
    existing: set[str],
    tags: list[str],
    rationale_prefix: str,
) -> list[Candidate]:
    if not remainders:
        remainders = ["A 1"]
    pattern = _pick_pattern(remainders)
    generated: list[Candidate] = []
    num_start = pattern.number + 1 if pattern.number is not None else 1
    index = 0
    while len(generated) < count:
        if pattern.number is None:
            remainder = f"{pattern.prefix} {num_start + index}".strip()
        else:
            remainder = f"{pattern.prefix}{num_start + index}{pattern.suffix}".strip()
        name = f"{family_key} {remainder}".strip()
        index += 1
        if name in existing:
            continue
        score = 0.6
        rationale = f"{rationale_prefix}: {family_key}"
        generated.append(_candidate_from_seed(name, score, None, list(tags), rationale))
    return generated


class _Pattern:
    def __init__(self, prefix: str, number: Optional[int], suffix: str) -> None:
        self.prefix = prefix
        self.number = number
        self.suffix = suffix


def _pick_pattern(remainders: list[str]) -> _Pattern:
    normalized = Counter(_normalize_pattern(rem) for rem in remainders)
    pattern_key, _ = normalized.most_common(1)[0]
    sample = next(rem for rem in remainders if _normalize_pattern(rem) == pattern_key)
    match = re.match(r"^(.*?)(\d+)([^0-9]*)$", sample)
    if match:
        return _Pattern(match.group(1), int(match.group(2)), match.group(3))
    return _Pattern(sample, None, "")


def _normalize_pattern(remainder: str) -> str:
    return re.sub(r"\d+", "{n}", remainder.strip())


def _score_candidates(candidates: list[Candidate], families: dict[str, list[str]]) -> list[tuple[Candidate, float, str]]:
    family_freq = {key: len(vals) for key, vals in families.items()}
    max_freq = max(family_freq.values(), default=1)
    scored: list[tuple[Candidate, float, str]] = []
    for candidate in candidates:
        family = _family_key(candidate.system_name)
        freq_score = (family_freq.get(family, 1) / max_freq) * 0.6
        plausibility = 0.2 if re.search(r"\d", candidate.system_name) else 0.05
        base = 0.1
        score = base + freq_score + plausibility
        rationale = f"{candidate.rationale} | freq={freq_score:.2f}, plaus={plausibility:.2f}"
        candidate.rationale = rationale
        scored.append((candidate, score, family))
    scored.sort(key=lambda item: (-item[1], item[2], item[0].system_name))
    return scored


def _apply_dispersion(scored: list[tuple[Candidate, float, str]]) -> list[Candidate]:
    family_counts: dict[str, int] = defaultdict(int)
    adjusted: list[tuple[Candidate, float]] = []
    for candidate, score, family in scored:
        penalty = 0.02 * family_counts[family]
        adjusted_score = score - penalty
        family_counts[family] += 1
        adjusted.append((candidate, adjusted_score))
    adjusted.sort(key=lambda item: (-item[1], item[0].system_name))
    return [item[0] for item in adjusted]


def _bucket_confidence(score: float) -> str:
    if score >= 0.75:
        return "HIGH"
    if score >= 0.4:
        return "MED"
    return "LOW"


def _emit(progress_cb: Optional[callable], progress: JobProgress) -> None:
    if progress_cb:
        progress_cb(progress)

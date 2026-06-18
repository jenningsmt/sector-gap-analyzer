"""Strategic planner models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Candidate:
    system_name: str
    score: float
    confidence: str
    distance_ly: Optional[float] = None
    tags: List[str] = field(default_factory=list)
    rationale: str = ""


@dataclass
class JobParams:
    mode: str
    sector: Optional[str] = None
    center_system: Optional[str] = None
    radius_ly: Optional[float] = None
    sub_sector: Optional[str] = None
    mass_code: Optional[str] = None


@dataclass
class JobProgress:
    phase: str
    current: int
    total: int
    message: str

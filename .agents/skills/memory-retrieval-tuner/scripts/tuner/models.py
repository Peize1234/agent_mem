from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Requirement:
    members: tuple[str, ...]
    raw_text: str

    @property
    def is_or(self) -> bool:
        return len(self.members) > 1


@dataclass(frozen=True)
class Turn:
    session_id: str
    session_code: str
    turn_index: int
    query_id: str
    question: str
    answer: str
    gold_raw: str
    requirements: tuple[Requirement, ...]
    required_context: str = ""
    dependency_type: str = ""


@dataclass(frozen=True)
class Dataset:
    path: str
    sha256: str
    sessions: dict[str, tuple[Turn, ...]]


@dataclass(frozen=True)
class Candidate:
    name: str
    stage: str
    config: dict[str, Any]
    provenance: dict[str, Any] = field(default_factory=dict)
    complexity: int = 0


@dataclass
class CandidateResult:
    name: str
    candidate_hash: str
    stage: str
    config: dict[str, Any]
    metrics: dict[str, Any]
    requirement_rows: list[dict[str, Any]]
    session_rows: list[dict[str, Any]]
    runtime_seconds: float
    work_seconds: float
    cache_hits: int
    cache_misses: int
    llm_calls: int = 0
    embedding_calls: int = 0
    reused_artifacts: list[str] = field(default_factory=list)
    complexity: int = 0
    status: str = "VALID"

    def serializable(self) -> dict[str, Any]:
        return asdict(self)

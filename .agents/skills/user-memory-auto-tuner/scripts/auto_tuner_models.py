from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RawMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    rowid: int
    message_id: str
    role: Literal["user", "assistant"]
    content: str
    name: str | None = None
    created_at: str | None = None
    turn_index: int
    status: str


class ConversationTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_turn_index: int
    messages: tuple[RawMessage, ...]
    question: str
    answer: str


class ExtractedSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_scope: str
    user_id: str
    run_id: str | None = None
    agent_id: str | None = None
    turns: tuple[ConversationTurn, ...]


class ExtractedHistory(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    history_db_path: str
    sessions: tuple[ExtractedSession, ...]
    incomplete_turn_count: int = 0

    @property
    def qa_turn_count(self) -> int:
        return sum(len(session.turns) for session in self.sessions)


class DependencyRequirement(BaseModel):
    """One necessary fact; multiple dependency IDs are equivalent OR sources."""

    model_config = ConfigDict(frozen=True)

    dependency_ids: tuple[str, ...] = Field(min_length=1)
    required_contexts: tuple[str, ...] = Field(min_length=1)


class DependencyLabel(BaseModel):
    model_config = ConfigDict(frozen=True)

    needs_history: bool
    requirements: tuple[DependencyRequirement, ...] = ()
    dependency_type: str = ""
    confidence: float = Field(ge=0, le=1)


class VerificationVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    dependencies_necessary: bool
    context_supported: bool
    logic_valid: bool
    confidence: float = Field(ge=0, le=1)
    issues: tuple[str, ...] = ()


class AnnotatedTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    benchmark_id: str
    source_turn_index: int
    question: str
    answer: str
    label: DependencyLabel
    verifier: VerificationVerdict | None = None
    status: Literal["VALID", "INDEPENDENT", "INVALID"]
    validation_issues: tuple[str, ...] = ()


class AnnotatedSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    benchmark_session_id: str
    source_session_scope: str
    user_id: str
    run_id: str | None = None
    agent_id: str | None = None
    turns: tuple[AnnotatedTurn, ...]


class AnnotationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    sessions: tuple[AnnotatedSession, ...]

    @property
    def valid_dependency_samples(self) -> int:
        return sum(
            turn.status == "VALID" and turn.label.needs_history for session in self.sessions for turn in session.turns
        )

    @property
    def filtered_samples(self) -> int:
        return sum(turn.status == "INVALID" for session in self.sessions for turn in session.turns)

    @property
    def qa_turn_count(self) -> int:
        return sum(len(session.turns) for session in self.sessions)

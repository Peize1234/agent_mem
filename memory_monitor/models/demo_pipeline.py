from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class PipelineStep(str, Enum):
    CAPTURE_INPUT = "capture_input"
    RETRIEVE_CONTEXT = "retrieve_context"
    AGENTIC_RETRIEVAL = "agentic_retrieval"
    BUILD_PROMPT = "build_prompt"
    GENERATE_RESPONSE = "generate_response"
    RUN_SHORTTERM = "run_shortterm"
    RUN_MIDTERM = "run_midterm"
    RUN_LONGTERM = "run_longterm"
    RUN_PROFILE = "run_profile"
    COMPLETE_TURN = "complete_turn"


class StepStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


# These names are shared by the monitor state reader and the UI.  They are
# deliberately separate from the persisted Demo pipeline steps: core jobs are
# created by ``Memory.add`` and are not owned by the Demo scheduler.
MEMORY_STATE_SECTIONS = (
    "short_term",
    "midterm_sessions",
    "midterm_pages",
    "fine_grained_longterm",
    "promoted_longterm",
    "profile",
    "migration_jobs",
    "longterm_extraction_jobs",
    "profile_jobs",
    "promotion_jobs",
)
CORE_JOB_TYPES = (
    "migration",
    "longterm_extraction",
    "profile",
    "promotion",
)
CORE_JOB_ACTIVE_STATUSES = frozenset({"pending", "queued", "running", "retry"})


FOREGROUND_STEPS = (
    PipelineStep.CAPTURE_INPUT,
    PipelineStep.RETRIEVE_CONTEXT,
    PipelineStep.AGENTIC_RETRIEVAL,
    PipelineStep.BUILD_PROMPT,
    PipelineStep.GENERATE_RESPONSE,
)
MEMORY_STEPS = (
    PipelineStep.RUN_SHORTTERM,
    PipelineStep.RUN_MIDTERM,
    PipelineStep.RUN_LONGTERM,
    PipelineStep.RUN_PROFILE,
)
BACKGROUND_STEPS = MEMORY_STEPS
PIPELINE_STEPS = (*FOREGROUND_STEPS, *MEMORY_STEPS)
VISIBLE_PIPELINE_STEPS = (*PIPELINE_STEPS, PipelineStep.COMPLETE_TURN)
OPTIONAL_PIPELINE_STEPS = frozenset(MEMORY_STEPS)
TERMINAL_STEP_STATUSES = frozenset({StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value})
INFLIGHT_STEP_STATUSES = frozenset({StepStatus.QUEUED.value, StepStatus.RUNNING.value})

STEP_DEPENDENCIES = {
    PipelineStep.CAPTURE_INPUT: (),
    PipelineStep.RETRIEVE_CONTEXT: (PipelineStep.CAPTURE_INPUT,),
    PipelineStep.AGENTIC_RETRIEVAL: (PipelineStep.RETRIEVE_CONTEXT,),
    PipelineStep.BUILD_PROMPT: (PipelineStep.AGENTIC_RETRIEVAL,),
    PipelineStep.GENERATE_RESPONSE: (PipelineStep.BUILD_PROMPT,),
    PipelineStep.RUN_SHORTTERM: (PipelineStep.GENERATE_RESPONSE,),
    # Memory.add() in RUN_SHORTTERM creates the real core job IDs consumed by
    # the other three branches. The graph keeps the branches visually parallel
    # while their node detail exposes this persisted backend dependency.
    PipelineStep.RUN_MIDTERM: (PipelineStep.RUN_SHORTTERM,),
    PipelineStep.RUN_LONGTERM: (PipelineStep.RUN_SHORTTERM,),
    PipelineStep.RUN_PROFILE: (PipelineStep.RUN_SHORTTERM,),
    PipelineStep.COMPLETE_TURN: MEMORY_STEPS,
}

_STEP_CONFIG_FIELDS = {
    PipelineStep.RUN_SHORTTERM: "run_shortterm",
    PipelineStep.RUN_MIDTERM: "run_midterm",
    PipelineStep.RUN_LONGTERM: "run_longterm",
    PipelineStep.RUN_PROFILE: "run_profile",
}


@dataclass(frozen=True)
class BackgroundStepConfig:
    run_midterm: bool = True
    run_longterm: bool = True
    run_profile: bool = True
    # Appended to retain the positional meaning of the three fields used by
    # older Demo tests and callers.
    run_shortterm: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> "BackgroundStepConfig":
        value = value or {}
        run_shortterm = bool(value.get("run_shortterm", True))
        if not run_shortterm:
            return cls(
                run_midterm=False,
                run_longterm=False,
                run_profile=False,
                run_shortterm=False,
            )
        return cls(
            run_midterm=bool(value.get("run_midterm", True)),
            run_longterm=bool(value.get("run_longterm", True)),
            run_profile=bool(value.get("run_profile", True)),
            run_shortterm=True,
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "run_shortterm": self.run_shortterm,
            "run_midterm": self.run_midterm,
            "run_longterm": self.run_longterm,
            "run_profile": self.run_profile,
        }

    def with_toggle(self, field: str, enabled: bool) -> "BackgroundStepConfig":
        """Apply one UI toggle while preserving the short-term dependency."""
        if field not in _STEP_CONFIG_FIELDS.values():
            raise ValueError(f"Unknown memory configuration field: {field}")
        values = self.as_dict()
        values[field] = bool(enabled)
        if field == "run_shortterm" and not enabled:
            values.update(
                run_midterm=False,
                run_longterm=False,
                run_profile=False,
            )
        elif field != "run_shortterm" and enabled:
            values["run_shortterm"] = True
        return BackgroundStepConfig.from_mapping(values)

    def enabled(self, step: PipelineStep | str) -> bool:
        step = PipelineStep(step)
        field = _STEP_CONFIG_FIELDS.get(step)
        return True if field is None else bool(getattr(self, field))

    def enabled_background_steps(self) -> tuple[PipelineStep, ...]:
        return tuple(step for step in BACKGROUND_STEPS if self.enabled(step))


def job_section_for_type(job_type: str) -> str:
    """Map a production core job type to its monitor state section."""
    return {
        "migration": "migration_jobs",
        "longterm_extraction": "longterm_extraction_jobs",
        "profile": "profile_jobs",
        "promotion": "promotion_jobs",
    }[job_type]


DEFAULT_BACKGROUND_CONFIG = BackgroundStepConfig()


def dependencies_for(
    step: PipelineStep | str,
    background_config: BackgroundStepConfig | Mapping[str, object] | None = None,
) -> tuple[PipelineStep, ...]:
    step = PipelineStep(step)
    return STEP_DEPENDENCIES[step]

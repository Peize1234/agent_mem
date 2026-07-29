from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class PipelineStep(str, Enum):
    CAPTURE_INPUT = "capture_input"
    RETRIEVE_CONTEXT = "retrieve_context"
    BUILD_PROMPT = "build_prompt"
    GENERATE_RESPONSE = "generate_response"
    COMMIT_TURN = "commit_turn"
    RUN_MIDTERM = "run_midterm"
    RUN_LONGTERM = "run_longterm"
    RUN_PROFILE = "run_profile"
    REFRESH_STATE = "refresh_state"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


FOREGROUND_STEPS = (
    PipelineStep.CAPTURE_INPUT,
    PipelineStep.RETRIEVE_CONTEXT,
    PipelineStep.BUILD_PROMPT,
    PipelineStep.GENERATE_RESPONSE,
    PipelineStep.COMMIT_TURN,
)
BACKGROUND_STEPS = (
    PipelineStep.RUN_MIDTERM,
    PipelineStep.RUN_LONGTERM,
    PipelineStep.RUN_PROFILE,
)
PIPELINE_STEPS = (*FOREGROUND_STEPS, *BACKGROUND_STEPS, PipelineStep.REFRESH_STATE)
OPTIONAL_PIPELINE_STEPS = frozenset(BACKGROUND_STEPS)

STEP_DEPENDENCIES = {
    PipelineStep.CAPTURE_INPUT: (),
    PipelineStep.RETRIEVE_CONTEXT: (PipelineStep.CAPTURE_INPUT,),
    PipelineStep.BUILD_PROMPT: (PipelineStep.RETRIEVE_CONTEXT,),
    PipelineStep.GENERATE_RESPONSE: (PipelineStep.BUILD_PROMPT,),
    PipelineStep.COMMIT_TURN: (PipelineStep.GENERATE_RESPONSE,),
    PipelineStep.RUN_MIDTERM: (PipelineStep.COMMIT_TURN,),
    PipelineStep.RUN_LONGTERM: (PipelineStep.COMMIT_TURN,),
    PipelineStep.RUN_PROFILE: (PipelineStep.COMMIT_TURN,),
    PipelineStep.REFRESH_STATE: BACKGROUND_STEPS,
}

_STEP_CONFIG_FIELDS = {
    PipelineStep.RUN_MIDTERM: "run_midterm",
    PipelineStep.RUN_LONGTERM: "run_longterm",
    PipelineStep.RUN_PROFILE: "run_profile",
}


@dataclass(frozen=True)
class BackgroundStepConfig:
    run_midterm: bool = True
    run_longterm: bool = True
    run_profile: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> "BackgroundStepConfig":
        value = value or {}
        return cls(
            run_midterm=bool(value.get("run_midterm", True)),
            run_longterm=bool(value.get("run_longterm", True)),
            run_profile=bool(value.get("run_profile", True)),
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "run_midterm": self.run_midterm,
            "run_longterm": self.run_longterm,
            "run_profile": self.run_profile,
        }

    def enabled(self, step: PipelineStep | str) -> bool:
        step = PipelineStep(step)
        field = _STEP_CONFIG_FIELDS.get(step)
        return True if field is None else bool(getattr(self, field))

    def enabled_background_steps(self) -> tuple[PipelineStep, ...]:
        return tuple(step for step in BACKGROUND_STEPS if self.enabled(step))


DEFAULT_BACKGROUND_CONFIG = BackgroundStepConfig()


def dependencies_for(
    step: PipelineStep | str,
    background_config: BackgroundStepConfig | Mapping[str, object] | None = None,
) -> tuple[PipelineStep, ...]:
    step = PipelineStep(step)
    dependencies = STEP_DEPENDENCIES[step]
    if step is not PipelineStep.REFRESH_STATE:
        return dependencies
    config = (
        background_config
        if isinstance(background_config, BackgroundStepConfig)
        else BackgroundStepConfig.from_mapping(background_config)
    )
    return tuple(dependency for dependency in dependencies if config.enabled(dependency))

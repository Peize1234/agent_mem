from __future__ import annotations

from enum import Enum


class PipelineStep(str, Enum):
    CAPTURE_INPUT = "capture_input"
    RETRIEVE_CONTEXT = "retrieve_context"
    BUILD_PROMPT = "build_prompt"
    GENERATE_RESPONSE = "generate_response"
    COMMIT_TURN = "commit_turn"
    RUN_MIGRATION = "run_migration"
    RUN_PROFILE = "run_profile"
    REFRESH_STATE = "refresh_state"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


PIPELINE_STEPS = tuple(PipelineStep)
OPTIONAL_PIPELINE_STEPS = frozenset(
    {
        PipelineStep.RUN_MIGRATION,
        PipelineStep.RUN_PROFILE,
    }
)

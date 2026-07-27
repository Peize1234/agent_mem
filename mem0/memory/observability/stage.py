from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional

from mem0.memory.observability.events import ObservationEvent
from mem0.memory.observability.sink import NoOpObservationSink, ObservationSink, emit_safely


@dataclass(slots=True)
class ObservationContext:
    trace_id: str
    capture_payloads: bool = True
    max_payload_length: int = 20000
    job_id: Optional[str] = None
    job_type: Optional[str] = None
    user_id: Optional[str] = None
    run_id: Optional[str] = None
    session_scope: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def event(
        self,
        *,
        stage: str,
        event_type: str,
        status: str,
        duration_ms: Optional[float] = None,
        error: Optional[BaseException] = None,
        **payloads: Any,
    ) -> ObservationEvent:
        return ObservationEvent.create(
            trace_id=self.trace_id,
            stage=stage,
            event_type=event_type,
            status=status,
            capture_payloads=self.capture_payloads,
            max_payload_length=self.max_payload_length,
            job_id=self.job_id,
            job_type=self.job_type,
            user_id=self.user_id,
            run_id=self.run_id,
            session_scope=self.session_scope,
            duration_ms=duration_ms,
            error_type=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
            **payloads,
            **self.extra,
        )


@contextmanager
def observation_stage(
    sink: ObservationSink,
    context: Optional[ObservationContext],
    stage: str,
    *,
    started_event: Optional[str] = None,
    succeeded_event: Optional[str] = None,
    failed_event: Optional[str] = None,
    input_data: Any = None,
) -> Iterator[Dict[str, Any]]:
    """Emit start/success/failure events and elapsed time around one stage."""

    state: Dict[str, Any] = {}
    if context is None or isinstance(sink, NoOpObservationSink):
        yield state
        return

    started_at = time.perf_counter()
    emit_safely(
        sink,
        lambda: context.event(
            stage=stage,
            event_type=started_event or f"{stage}.started",
            status="started",
            input_data=input_data,
        ),
    )
    try:
        yield state
    except Exception as exc:
        duration_ms = (time.perf_counter() - started_at) * 1000
        emit_safely(
            sink,
            lambda exc=exc: context.event(
                stage=stage,
                event_type=failed_event or f"{stage}.failed",
                status="failed",
                duration_ms=duration_ms,
                error=exc,
                output_data=state.get("output"),
                before_data=state.get("before"),
                after_data=state.get("after"),
            ),
        )
        raise
    else:
        duration_ms = (time.perf_counter() - started_at) * 1000
        emit_safely(
            sink,
            lambda: context.event(
                stage=stage,
                event_type=succeeded_event or f"{stage}.succeeded",
                status="succeeded",
                duration_ms=duration_ms,
                output_data=state.get("output"),
                before_data=state.get("before"),
                after_data=state.get("after"),
            ),
        )

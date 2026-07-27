from __future__ import annotations

import logging
from typing import Callable, Protocol, Union

from mem0.memory.observability.events import ObservationEvent

logger = logging.getLogger(__name__)


class ObservationSink(Protocol):
    def emit(self, event: ObservationEvent) -> None:
        """Persist or forward an observation event."""


class NoOpObservationSink:
    """Default sink used when observability is disabled."""

    def emit(self, event: ObservationEvent) -> None:
        return None


ObservationEventFactory = Callable[[], ObservationEvent]


def emit_safely(
    sink: ObservationSink,
    event: Union[ObservationEvent, ObservationEventFactory],
) -> None:
    """Best-effort event construction and emission that never changes business behavior."""

    if isinstance(sink, NoOpObservationSink):
        return

    try:
        observation = event() if callable(event) else event
        sink.emit(observation)
    except Exception:
        event_type = getattr(locals().get("observation"), "event_type", "unknown")
        logger.warning("Observation sink failed for event_type=%s", event_type, exc_info=True)

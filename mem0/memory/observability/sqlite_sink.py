from __future__ import annotations

from mem0.memory.observability.events import ObservationEvent


class SQLiteObservationSink:
    """Stores normalized observations through the existing SQLite manager."""

    def __init__(self, db):
        self.db = db

    def emit(self, event: ObservationEvent) -> None:
        self.db.insert_observation_event(event.as_record())

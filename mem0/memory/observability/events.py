from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from mem0.utils.timestamps import beijing_now_iso


def _json_safe(value: Any, *, max_length: int) -> Optional[str]:
    if value is None:
        return None
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        try:
            serialized = json.dumps(repr(value), ensure_ascii=False)
        except Exception:
            serialized = '"<unserializable>"'
    if max_length and len(serialized) > max_length:
        envelope = {"_truncated": True, "_original_length": len(serialized), "preview": ""}
        empty_envelope = json.dumps(envelope, ensure_ascii=False)
        if len(empty_envelope) <= max_length:
            preview_length = max_length - len(empty_envelope)
            while preview_length:
                envelope["preview"] = serialized[:preview_length]
                encoded = json.dumps(envelope, ensure_ascii=False)
                if len(encoded) <= max_length:
                    return encoded
                preview_length -= 1
            return empty_envelope
        if max_length == 1:
            return "0"
        preview_length = max_length - 2
        while preview_length:
            encoded = json.dumps(serialized[:preview_length], ensure_ascii=False)
            if len(encoded) <= max_length:
                return encoded
            preview_length -= 1
        return '""'
    return serialized


@dataclass(slots=True)
class ObservationEvent:
    """One normalized event in a memory processing trace."""

    trace_id: str
    stage: str
    event_type: str
    status: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    job_id: Optional[str] = None
    job_type: Optional[str] = None
    user_id: Optional[str] = None
    run_id: Optional[str] = None
    session_scope: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    duration_ms: Optional[float] = None
    input_json: Optional[str] = None
    output_json: Optional[str] = None
    before_json: Optional[str] = None
    after_json: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str = field(default_factory=beijing_now_iso)

    @classmethod
    def create(
        cls,
        *,
        trace_id: str,
        stage: str,
        event_type: str,
        status: str,
        capture_payloads: bool = True,
        max_payload_length: int = 20000,
        input_data: Any = None,
        output_data: Any = None,
        before_data: Any = None,
        after_data: Any = None,
        **context: Any,
    ) -> "ObservationEvent":
        payload = {
            "input_json": input_data,
            "output_json": output_data,
            "before_json": before_data,
            "after_json": after_data,
        }
        if capture_payloads:
            payload = {key: _json_safe(value, max_length=max_payload_length) for key, value in payload.items()}
        else:
            payload = {key: None for key in payload}
        return cls(
            trace_id=trace_id,
            stage=stage,
            event_type=event_type,
            status=status,
            **payload,
            **{key: value for key, value in context.items() if key in cls.__dataclass_fields__},
        )

    def as_record(self) -> Dict[str, Any]:
        return asdict(self)

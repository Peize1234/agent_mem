#!/usr/bin/env python3
"""Trace the end-to-end answer-prompt -> external answer -> add workflow for Sessions 2 and 4."""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import inspect
import json
import os
import sqlite3
import sys
import sysconfig
import time
import traceback
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

os.environ.setdefault("MEM0_TELEMETRY", "false")
os.environ.setdefault("POSTHOG_DISABLED", "true")

PACKAGE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from mem0.utils.timestamps import beijing_now, beijing_now_iso  # noqa: E402

try:
    from .run_agent_mem_single_session_test import build_memory as build_base_memory
    from .run_agent_mem_single_session_test import normalize_llm_response, pair_turns
except ImportError:  # Direct script execution.
    from run_agent_mem_single_session_test import build_memory as build_base_memory
    from run_agent_mem_single_session_test import normalize_llm_response, pair_turns


DEFAULT_DATASET_PATH = PACKAGE_ROOT / "finance_memory_single_session_timeline.json"
DEFAULT_OUTPUT_ROOT = PACKAGE_ROOT / "retrieve_context_add_trace_outputs"
DEFAULT_STATE_ROOT = PACKAGE_ROOT / "results" / "retrieve_context_add_trace_state"
DEFAULT_USER_ID = "finance-profile-test-user"
SELECTED_SESSION_INDEXES = (1, 3)
EXPECTED_TURN_COUNT = 10
SENSITIVE_FIELD_NAMES = {
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
    "credentials",
}
SENSITIVE_FIELD_SUFFIXES = ("_api_key", "_password", "_secret", "_token", "_credentials")
VECTOR_FIELD_NAMES = {"embedding", "embeddings", "vector", "vectors"}


def _is_sensitive_field(name: Any) -> bool:
    normalized = str(name).lower()
    return normalized in SENSITIVE_FIELD_NAMES or normalized.endswith(SENSITIVE_FIELD_SUFFIXES)


def make_json_serializable(value: Any, _seen: Optional[set[int]] = None) -> Any:
    """Convert common application objects to safe JSON-compatible values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, Path, Enum)):
        return value.value if isinstance(value, Enum) else str(value)

    seen = _seen if _seen is not None else set()
    value_id = id(value)
    if value_id in seen:
        return f"<{type(value).__name__}:recursive>"
    seen.add(value_id)

    try:
        if isinstance(value, sqlite3.Row):
            value = {key: value[key] for key in value.keys()}
        elif hasattr(value, "model_dump"):
            value = value.model_dump()
        elif is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)

        if isinstance(value, dict):
            return {
                str(key): "***REDACTED***" if _is_sensitive_field(key) else make_json_serializable(item, seen)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [make_json_serializable(item, seen) for item in value]

        payload = getattr(value, "payload", None)
        if isinstance(payload, dict):
            return {
                "id": make_json_serializable(getattr(value, "id", None), seen),
                "payload": make_json_serializable(payload, seen),
                "metadata": make_json_serializable(getattr(value, "metadata", None), seen),
                "score": make_json_serializable(getattr(value, "score", None), seen),
            }
        if hasattr(value, "__dict__"):
            public_values = {
                key: item for key, item in vars(value).items() if not key.startswith("_") and not callable(item)
            }
            if public_values:
                return make_json_serializable(public_values, seen)
        return f"<{type(value).__name__}>"
    finally:
        seen.discard(value_id)


def json_text(value: Any) -> str:
    return json.dumps(make_json_serializable(value), ensure_ascii=False, indent=2, default=str)


def _compact_json_text(value: Any) -> str:
    return json.dumps(
        make_json_serializable(value),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(content, encoding="utf-8")
    temporary_path.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json_text(value) + "\n")


def parse_environment_flag(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid MEM0_TEST_MODE value: {value!r}")


def resolve_test_mode(cli_value: Optional[bool], environment_value: Optional[str] = None) -> bool:
    """Resolve test mode with CLI > environment > False precedence."""
    if cli_value is not None:
        return cli_value
    environment_mode = parse_environment_flag(
        os.getenv("MEM0_TEST_MODE") if environment_value is None else environment_value
    )
    return environment_mode if environment_mode is not None else False


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--test-mode", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--threshold", type=float, default=0.1)
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--short-term-capacity", type=int, default=4)
    parser.add_argument("--top-k-mid-topic", type=int, default=5)
    parser.add_argument("--top-k-mid-pages", type=int, default=5)
    parser.add_argument("--max-total-pages", type=int, default=5)
    parser.add_argument("--session-similarity-threshold", type=float, default=0.50)
    parser.add_argument("--midterm-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--deepseek-model", default=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--embedding-model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--embedding-dims", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=2400)
    parser.add_argument("--qdrant-path", default=None)
    parser.add_argument("--history-db-path", default=None)
    parser.add_argument("--collection-name", default=None)
    parser.add_argument("--run-name", default="retrieve_context_add_trace_state")
    return parser.parse_args(argv)


def load_selected_turns(
    dataset_path: Path,
    *,
    selected_indexes: Sequence[int] = SELECTED_SESSION_INDEXES,
    user_id: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    """Load user questions from Sessions 2 and 4 in their original order."""
    data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError("Dataset must contain a sessions array")
    if not selected_indexes or max(selected_indexes) >= len(sessions):
        raise ValueError(f"Dataset contains {len(sessions)} sessions; cannot select indexes {list(selected_indexes)}")

    dataset_user_id = (data.get("user") or {}).get("user_id")
    effective_user_id = user_id or dataset_user_id or DEFAULT_USER_ID
    selected_sessions = [sessions[index] for index in selected_indexes]
    turns: List[Dict[str, Any]] = []

    for session_order, session in zip((index + 1 for index in selected_indexes), selected_sessions):
        session_id = session.get("session_id")
        if not session_id:
            raise ValueError(f"Session {session_order} is missing session_id")
        for session_turn_index, (user_turn, assistant_turn) in enumerate(pair_turns(session), start=1):
            turns.append(
                {
                    "global_turn_index": len(turns) + 1,
                    "session_order": session_order,
                    "session_id": session_id,
                    "session_turn_index": session_turn_index,
                    "user_id": effective_user_id,
                    "query": user_turn["content"],
                    "reference_answer": assistant_turn["content"],
                }
            )

    if len(turns) != EXPECTED_TURN_COUNT:
        raise ValueError(
            f"Expected {EXPECTED_TURN_COUNT} user questions from selected Sessions 2 and 4; actual count: {len(turns)}"
        )
    return selected_sessions, turns, effective_user_id


def _caller_details(frame_info: inspect.FrameInfo) -> Dict[str, Any]:
    frame = frame_info.frame
    module_name = str(frame.f_globals.get("__name__") or "unknown")
    self_object = frame.f_locals.get("self")
    class_object = frame.f_locals.get("cls")
    if self_object is not None:
        class_name: Optional[str] = type(self_object).__name__
    elif isinstance(class_object, type):
        class_name = class_object.__name__
    else:
        class_name = None
    return {
        "caller_module": module_name,
        "caller_class": class_name,
        "caller_method": frame_info.function,
        "caller_file": str(Path(frame_info.filename).resolve()),
        "caller_line": frame_info.lineno,
    }


def _is_python_stdlib_file(filename: str) -> bool:
    try:
        path = Path(filename).resolve()
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            return False
        stdlib_path = Path(sysconfig.get_paths()["stdlib"]).resolve()
        path.relative_to(stdlib_path)
        return True
    except (KeyError, OSError, ValueError):
        return False


def detect_llm_caller() -> Dict[str, Any]:
    """Return the nearest relevant business frame without retaining the full stack."""
    stack = inspect.stack()
    fallback: Optional[Dict[str, Any]] = None
    script_file = Path(__file__).resolve()
    try:
        for frame_info in stack[1:]:
            try:
                frame_file = Path(frame_info.filename).resolve()
            except OSError:
                continue
            if frame_file == script_file or _is_python_stdlib_file(frame_info.filename):
                continue

            details = _caller_details(frame_info)
            module_name = details["caller_module"]
            class_name = details["caller_class"]
            if class_name == "TracedLLM" or module_name in {"inspect", "contextlib", "contextvars"}:
                continue
            if module_name.startswith("mem0."):
                return details
            if fallback is None:
                fallback = details
        return fallback or {
            "caller_module": "unknown",
            "caller_class": None,
            "caller_method": "unknown",
            "caller_file": "unknown",
            "caller_line": None,
        }
    finally:
        del stack


def classify_llm_source(
    module_name: str,
    class_name: Optional[str],
    method_name: str,
    phase: str,
) -> str:
    """Map a selected business frame to a stable LLM source category."""
    module = (module_name or "").lower()
    class_value = (class_name or "").lower()
    method = (method_name or "").lower()

    if method in {"generate_final_answer", "generate_answer"}:
        return "answer_generation"
    if phase == "answer_generation":
        return "answer_generation"
    if method == "_create_procedural_memory" or "procedural_memory" in method:
        return "procedural_memory"
    if "midterm" in module or class_value in {"midtermupdater", "midtermmemory"}:
        return "midterm_memory"
    if (
        "profile_updater" in module
        or class_value == "profileupdater"
        or method in {"generate_update_plan", "generate_update_plan_async"}
    ):
        return "user_profile"
    if (
        method in {"_add_to_vector_store", "_process_evicted_long_term_memories"}
        or "long_term" in method
        or "longterm" in method
        or (module == "mem0.memory.main" and method in {"_extract_memories", "_update_memory"})
    ):
        return "long_term_memory"
    if phase == "retrieve_context":
        return "retrieve_context"
    return "unknown"


def _caller_label(call: Mapping[str, Any]) -> str:
    class_name = call.get("caller_class")
    method_name = call.get("caller_method") or "unknown"
    return f"{class_name}.{method_name}" if class_name else str(method_name)


def _render_fenced_value(value: Any) -> str:
    if isinstance(value, str):
        return f"````text\n{value}\n````"
    return f"````json\n{json_text(value)}\n````"


def render_llm_messages(messages: Any) -> str:
    """Render message content as raw text so real newlines remain visible."""
    if messages is None:
        message_list: List[Any] = []
    elif isinstance(messages, Mapping):
        message_list = [messages]
    elif isinstance(messages, (str, bytes)):
        message_list = [{"role": "unknown", "content": messages.decode() if isinstance(messages, bytes) else messages}]
    else:
        try:
            message_list = list(messages)
        except TypeError:
            message_list = [messages]

    if not message_list:
        return "#### Prompt Messages\n\n当前调用没有 Prompt Messages。"

    parts = ["#### Prompt Messages"]
    for index, message in enumerate(message_list, start=1):
        if isinstance(message, Mapping):
            role = message.get("role", "unknown")
            content = message.get("content", "")
            metadata = {key: value for key, value in message.items() if key not in {"role", "content"}}
        else:
            role = "unknown"
            content = message
            metadata = {}
        escaped_role = str(role).replace("`", "\\`")

        parts.extend(
            [
                f"##### Message {index}",
                f"- Role: `{escaped_role}`",
                _render_fenced_value(content),
            ]
        )
        if metadata:
            parts.extend(["- Message Metadata:", f"```json\n{json_text(metadata)}\n```"])
    return "\n\n".join(parts)


class TraceRecorder:
    """Collect LLM calls and emit debug output only when test mode is enabled."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.calls: List[Dict[str, Any]] = []
        self.evictions: List[Dict[str, Any]] = []
        self._phase = contextvars.ContextVar("trace_phase", default="unscoped")
        self._turn = contextvars.ContextVar("trace_turn", default=None)
        self._llm_source = contextvars.ContextVar("trace_llm_source", default=None)

    @contextlib.contextmanager
    def phase(self, name: str):
        token = self._phase.set(name)
        try:
            yield
        finally:
            self._phase.reset(token)

    @contextlib.contextmanager
    def turn(self, index: int):
        token = self._turn.set(index)
        try:
            yield
        finally:
            self._turn.reset(token)

    @contextlib.contextmanager
    def llm_source(self, **source: Any):
        token = self._llm_source.set({key: value for key, value in source.items() if value is not None})
        try:
            yield
        finally:
            self._llm_source.reset(token)

    @property
    def current_phase(self) -> str:
        return self._phase.get()

    @property
    def current_llm_source(self) -> Optional[Dict[str, Any]]:
        return self._llm_source.get()

    def record_call(self, call: Dict[str, Any]) -> None:
        self.calls.append(call)
        if not self.enabled:
            return
        turn = self._turn.get()
        prefix = f"[TURN {turn:02d}]" if turn is not None else "[GLOBAL]"
        marker = "\n".join(
            [
                prefix,
                f"[LLM CALL {call['call_index']:02d}]",
                f"[PHASE={call['phase']}]",
                f"[SOURCE={call['source_type']}]",
                f"[CALLER={_caller_label(call)}]",
            ]
        )
        print(f"\n{marker}\n[PROMPT]\n\n{render_llm_messages(call['messages'])}")
        response = call["raw_response"] if call.get("error") is None else call["error"]
        print(f"\n{marker}\n[RESPONSE]\n\n{_render_fenced_value(response)}")

    def record_eviction(self, evicted_messages: Any, *, midterm_triggered: bool) -> None:
        if not self.enabled:
            return
        messages = make_json_serializable(evicted_messages or [])
        self.evictions.append(
            {
                "turn": self._turn.get(),
                "evicted_count": len(messages),
                "evicted_messages": messages,
                "midterm_triggered": bool(messages) and midterm_triggered,
                "long_term_triggered": bool(messages),
            }
        )

    def emit(self, marker: str, value: Any) -> None:
        if self.enabled:
            print(f"\n{marker}\n{json_text(value)}")

    def emit_llm_messages(self, marker: str, messages: Any) -> None:
        if self.enabled:
            print(f"\n{marker}\n\n{render_llm_messages(messages)}")

    def emit_llm_response(self, marker: str, response: Any) -> None:
        if self.enabled:
            print(f"\n{marker}\n\n{_render_fenced_value(response)}")

    def emit_database_snapshot(self, marker: str, snapshot: Dict[str, Any]) -> None:
        if self.enabled:
            print(f"\n{marker}\n\n{render_database_snapshot(snapshot)}")

    def emit_no_llm_calls(self, phase: str, call_start: int) -> None:
        if not any(call.get("phase") == phase for call in self.calls[call_start:]):
            turn = self._turn.get()
            prefix = f"[TURN {turn:02d}]" if turn is not None else "[GLOBAL]"
            self.emit(f"{prefix}[PHASE={phase}][LLM]", "本阶段未调用 LLM")


class TracedLLM:
    """Test-only wrapper that records prompts, raw responses, and failures."""

    def __init__(self, wrapped: Any, recorder: TraceRecorder, *, provider: str, model: str) -> None:
        self._wrapped = wrapped
        self._recorder = recorder
        self._provider = provider
        self._model = model

    def _source_details(self) -> Dict[str, Any]:
        if not self._recorder.enabled:
            return {
                "source_type": "unknown",
                "caller_module": "unknown",
                "caller_class": None,
                "caller_method": "unknown",
                "caller_file": "unknown",
                "caller_line": None,
            }

        explicit_source = self._recorder.current_llm_source
        if explicit_source:
            details = {
                "caller_module": "unknown",
                "caller_class": None,
                "caller_method": "unknown",
                "caller_file": "unknown",
                "caller_line": None,
                **explicit_source,
            }
        else:
            details = detect_llm_caller()
        details["source_type"] = details.get("source_type") or classify_llm_source(
            str(details.get("caller_module") or ""),
            details.get("caller_class"),
            str(details.get("caller_method") or ""),
            self._recorder.current_phase,
        )
        return details

    def _new_call(self, args: Sequence[Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
        messages = kwargs.get("messages", args[0] if args else [])
        return {
            "call_index": len(self._recorder.calls) + 1,
            "phase": self._recorder.current_phase,
            **self._source_details(),
            "timestamp": beijing_now_iso(),
            "provider": self._provider,
            "model": self._model,
            "messages": make_json_serializable(messages),
            "response_format": make_json_serializable(kwargs.get("response_format")),
            "other_safe_kwargs": make_json_serializable(
                {key: value for key, value in kwargs.items() if key not in {"messages", "response_format"}}
            ),
            "raw_response": None,
            "error": None,
        }

    def generate_response(self, *args, **kwargs):
        call = self._new_call(args, kwargs)
        try:
            response = self._wrapped.generate_response(*args, **kwargs)
            call["raw_response"] = make_json_serializable(response)
            return response
        except Exception as exc:
            call["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            self._recorder.record_call(call)

    async def _generate_async(self, method_name: str, *args, **kwargs):
        call = self._new_call(args, kwargs)
        try:
            response = await getattr(self._wrapped, method_name)(*args, **kwargs)
            call["raw_response"] = make_json_serializable(response)
            return response
        except Exception as exc:
            call["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            self._recorder.record_call(call)

    def __getattr__(self, name: str):
        target = getattr(self._wrapped, name)
        if name in {"generate_response_async", "agenerate_response"} and inspect.iscoroutinefunction(target):

            async def traced_async(*args, **kwargs):
                return await self._generate_async(name, *args, **kwargs)

            return traced_async
        return target


def install_test_eviction_trace(memory: Any, recorder: TraceRecorder) -> None:
    """Record short-term evictions without changing the public add() result."""
    original_save = memory._save_short_term_messages

    def traced_save(messages, session_scope):
        evicted_messages = original_save(messages, session_scope)
        recorder.record_eviction(
            evicted_messages,
            midterm_triggered=bool(memory._midterm_enabled()),
        )
        return evicted_messages

    memory._save_short_term_messages = traced_save


def _vector_result_rows(result: Any) -> tuple[List[Any], Any]:
    if isinstance(result, tuple) and len(result) == 2:
        return list(result[0] or []), result[1]
    if isinstance(result, list) and len(result) == 2 and isinstance(result[0], list):
        return list(result[0]), result[1]
    if isinstance(result, (list, tuple)):
        return list(result), None
    return [], None


def _vector_record(row: Any) -> Dict[str, Any]:
    if isinstance(row, dict):
        return make_json_serializable(
            {
                "id": row.get("id"),
                "payload": row.get("payload"),
                "metadata": row.get("metadata"),
                "score": row.get("score"),
            }
        )
    return make_json_serializable(
        {
            "id": getattr(row, "id", None),
            "payload": getattr(row, "payload", None),
            "metadata": getattr(row, "metadata", None),
            "score": getattr(row, "score", None),
        }
    )


def _scroll_vector_client(store: Any, page_size: int = 256) -> List[Any]:
    client = getattr(store, "client", None)
    collection_name = getattr(store, "collection_name", None)
    scroll = getattr(client, "scroll", None)
    if not callable(scroll) or not collection_name:
        raise RuntimeError("Vector store pagination cursor is present but scroll() is unavailable")

    records: List[Any] = []
    offset = None
    while True:
        page, offset = scroll(
            collection_name=collection_name,
            limit=page_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        records.extend(page or [])
        if offset is None:
            return records


def _list_vector_store(store: Any, warnings: List[str], label: str) -> List[Dict[str, Any]]:
    try:
        result = store.list(filters=None, top_k=10000)
        rows, cursor = _vector_result_rows(result)
        if cursor is not None:
            rows = _scroll_vector_client(store)
        return [_vector_record(row) for row in rows]
    except Exception as exc:
        warning = f"{label} snapshot is incomplete: {type(exc).__name__}: {exc}"
        warnings.append(warning)
        return []


def _collect_sqlite_tables(memory: Any) -> Dict[str, List[Dict[str, Any]]]:
    db = memory.db
    connection = db.connection
    lock = getattr(db, "_lock", None)
    manager = lock if lock is not None else contextlib.nullcontext()
    with manager:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            if not str(row[0]).startswith("sqlite_")
        ]
        tables = {}
        for table_name in names:
            quoted_name = str(table_name).replace('"', '""')
            cursor = connection.execute(f'SELECT * FROM "{quoted_name}"')
            columns = [description[0] for description in cursor.description or []]
            tables[table_name] = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return make_json_serializable(tables)


def collect_full_database_snapshot(memory: Any, user_id: str = DEFAULT_USER_ID) -> Dict[str, Any]:
    """Collect all available SQLite, vector, mid-term, entity, and profile state."""
    warnings: List[str] = []
    snapshot: Dict[str, Any] = {
        "timestamp": beijing_now_iso(),
        "sqlite": {"tables": _collect_sqlite_tables(memory)},
        "long_term_memory": [],
        "midterm_memory": {"sessions": [], "pages": []},
        "entity_store": {"initialized": False, "records": []},
        "profile_view": {},
        "warnings": warnings,
    }

    vector_store = getattr(memory, "vector_store", None)
    if vector_store is not None:
        snapshot["long_term_memory"] = _list_vector_store(vector_store, warnings, "long-term vector store")

    midterm_enabled = bool(getattr(getattr(memory, "config", None), "midterm", None)) and bool(
        getattr(memory.config.midterm, "enabled", False)
    )
    if midterm_enabled:
        try:
            midterm = memory.midterm_memory
            snapshot["midterm_memory"] = {
                "sessions": [_vector_record(row) for row in midterm.list_sessions(top_k=10000)],
                "pages": [_vector_record(row) for row in midterm.list_pages(top_k=10000)],
            }
        except Exception as exc:
            warnings.append(f"mid-term snapshot is incomplete: {type(exc).__name__}: {exc}")

    entity_store = getattr(memory, "_entity_store", None)
    if entity_store is not None:
        snapshot["entity_store"] = {
            "initialized": True,
            "records": _list_vector_store(entity_store, warnings, "entity store"),
        }

    try:
        snapshot["profile_view"] = make_json_serializable(memory.get_profile(user_id, include_metadata=True))
    except Exception as exc:
        warnings.append(f"profile view is incomplete: {type(exc).__name__}: {exc}")
    return snapshot


def _markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        text = _compact_json_text(value)
    else:
        text = str(make_json_serializable(value))
    return text.replace("|", r"\|").replace("\r\n", "<br>").replace("\r", "<br>").replace("\n", "<br>")


def _ordered_columns(rows: Sequence[Mapping[str, Any]], preferred: Sequence[str] = ()) -> List[str]:
    columns: List[str] = []
    for column in preferred:
        if column not in columns:
            columns.append(column)
    for row in rows:
        for column in row:
            if column not in columns:
                columns.append(str(column))
    return columns


def _render_markdown_table(
    rows: Sequence[Mapping[str, Any]],
    *,
    columns: Optional[Sequence[str]] = None,
    empty_message: str = "当前表为空。",
) -> str:
    if not rows:
        return empty_message
    selected_columns = list(columns) if columns is not None else _ordered_columns(rows)
    header = "| " + " | ".join(_markdown_cell(column) for column in selected_columns) + " |"
    divider = "|" + "|".join("---" for _ in selected_columns) + "|"
    body = ["| " + " | ".join(_markdown_cell(row.get(column)) for column in selected_columns) + " |" for row in rows]
    return "\n".join([header, divider, *body])


def _mapping_rows(records: Any) -> List[Dict[str, Any]]:
    if not isinstance(records, (list, tuple)):
        return []
    return [dict(record) if isinstance(record, Mapping) else {"value": record} for record in records]


def _without_vector_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_vector_fields(item)
            for key, item in value.items()
            if str(key).lower() not in VECTOR_FIELD_NAMES
        }
    if isinstance(value, (list, tuple)):
        return [_without_vector_fields(item) for item in value]
    return value


def _payload_from_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        return _without_vector_fields(payload)
    return _without_vector_fields(
        {
            str(key): value
            for key, value in record.items()
            if key not in {"id", "metadata", "payload", "score"} and str(key).lower() not in VECTOR_FIELD_NAMES
        }
    )


def _render_sqlite_snapshot(sqlite_snapshot: Any) -> str:
    parts = ["## SQLite 数据库"]
    tables = sqlite_snapshot.get("tables", {}) if isinstance(sqlite_snapshot, Mapping) else {}
    if not isinstance(tables, Mapping) or not tables:
        parts.append("当前没有 SQLite 业务表。")
        return "\n\n".join(parts)

    for table_name, raw_rows in tables.items():
        rows = _mapping_rows(raw_rows)
        parts.extend(
            [
                f"### 表：{table_name}",
                f"记录数：{len(rows)}",
                _render_markdown_table(rows),
            ]
        )
    return "\n\n".join(parts)


def _render_long_term_snapshot(records: Any) -> str:
    raw_rows = _mapping_rows(records)
    columns = [
        "id",
        "memory/data",
        "user_id",
        "run_id",
        "agent_id",
        "role",
        "created_at",
        "updated_at",
        "expiration_date",
        "metadata",
        "other_payload",
    ]
    common_payload_fields = {
        "data",
        "memory",
        "user_id",
        "run_id",
        "agent_id",
        "role",
        "created_at",
        "updated_at",
        "expiration_date",
        "metadata",
    }
    rows = []
    for record in raw_rows:
        payload = _payload_from_record(record)
        other_payload = {key: value for key, value in payload.items() if key not in common_payload_fields}
        rows.append(
            {
                "id": record.get("id", payload.get("id")),
                "memory/data": payload.get("data", payload.get("memory")),
                "user_id": payload.get("user_id"),
                "run_id": payload.get("run_id"),
                "agent_id": payload.get("agent_id"),
                "role": payload.get("role"),
                "created_at": payload.get("created_at"),
                "updated_at": payload.get("updated_at"),
                "expiration_date": payload.get("expiration_date"),
                "metadata": _without_vector_fields(record.get("metadata", payload.get("metadata"))),
                "other_payload": other_payload or None,
            }
        )
    return "\n\n".join(
        [
            "## 长期记忆向量库",
            f"记录数：{len(rows)}",
            _render_markdown_table(rows, columns=columns, empty_message="当前长期记忆向量库为空。"),
        ]
    )


def _flatten_payload_records(records: Any) -> List[Dict[str, Any]]:
    rows = []
    for record in _mapping_rows(records):
        payload = _payload_from_record(record)
        row = {"id": record.get("id", payload.get("id")), **payload}
        row = {key: value for key, value in row.items() if str(key).lower() not in VECTOR_FIELD_NAMES}
        if record.get("metadata") is not None:
            row["metadata"] = _without_vector_fields(record["metadata"])
        rows.append(row)
    return rows


def _render_midterm_snapshot(midterm_snapshot: Any) -> str:
    midterm = midterm_snapshot if isinstance(midterm_snapshot, Mapping) else {}
    sessions = _flatten_payload_records(midterm.get("sessions", []))
    pages = _flatten_payload_records(midterm.get("pages", []))
    session_columns = _ordered_columns(
        sessions,
        ("id", "session_id", "topic", "summary", "heat", "created_at", "updated_at"),
    )
    page_columns = _ordered_columns(
        pages,
        ("id", "session_id", "page_id", "summary", "messages", "heat", "created_at", "updated_at"),
    )
    return "\n\n".join(
        [
            "## 中期记忆",
            "### Mid-term Sessions",
            f"记录数：{len(sessions)}",
            _render_markdown_table(sessions, columns=session_columns),
            "### Mid-term Pages",
            f"记录数：{len(pages)}",
            _render_markdown_table(pages, columns=page_columns),
        ]
    )


def _render_entity_snapshot(entity_snapshot: Any) -> str:
    entity = entity_snapshot if isinstance(entity_snapshot, Mapping) else {}
    initialized = bool(entity.get("initialized"))
    parts = ["## Entity Store", f"状态：{'已初始化' if initialized else '未初始化'}"]
    if not initialized:
        parts.append("Entity Store 尚未初始化。")
        return "\n\n".join(parts)

    records = _mapping_rows(entity.get("records", []))
    columns = [
        "id",
        "data",
        "entity_type",
        "linked_memory_ids",
        "user_id",
        "run_id",
        "created_at",
        "updated_at",
    ]
    rows = []
    for record in records:
        payload = _payload_from_record(record)
        rows.append(
            {
                "id": record.get("id", payload.get("id")),
                "data": payload.get("data"),
                "entity_type": payload.get("entity_type"),
                "linked_memory_ids": payload.get("linked_memory_ids"),
                "user_id": payload.get("user_id"),
                "run_id": payload.get("run_id"),
                "created_at": payload.get("created_at"),
                "updated_at": payload.get("updated_at"),
            }
        )
    parts.extend(
        [
            f"记录数：{len(rows)}",
            _render_markdown_table(rows, columns=columns, empty_message="当前 Entity Store 为空。"),
        ]
    )
    return "\n\n".join(parts)


def _profile_attributes(profile_view: Any) -> Dict[str, Any]:
    if not isinstance(profile_view, Mapping):
        return {}
    nested_profile = profile_view.get("profile")
    if isinstance(nested_profile, Mapping):
        return dict(nested_profile)
    return dict(profile_view)


def _render_profile_snapshot(profile_view: Any) -> str:
    attributes = _profile_attributes(profile_view)
    if not attributes:
        return "## 用户画像\n\n当前用户画像为空。"

    rows = []
    metadata_markers = {"value_version", "version", "updated_at", "source", "source_type"}
    for attribute, raw_value in attributes.items():
        if isinstance(raw_value, Mapping) and "value" in raw_value and metadata_markers.intersection(raw_value):
            current_value = raw_value.get("value")
            version = raw_value.get("value_version", raw_value.get("version"))
            updated_at = raw_value.get("updated_at")
            source = raw_value.get("source", raw_value.get("source_type"))
        else:
            current_value = raw_value
            version = updated_at = source = None
        rows.append(
            {
                "属性": attribute,
                "当前值": current_value,
                "版本": version,
                "更新时间": updated_at,
                "来源": source,
            }
        )
    return "\n\n".join(
        [
            "## 用户画像",
            _render_markdown_table(rows, columns=("属性", "当前值", "版本", "更新时间", "来源")),
        ]
    )


def _render_snapshot_warnings(warnings: Any) -> str:
    warning_list = list(warnings) if isinstance(warnings, (list, tuple)) else []
    if not warning_list:
        return "## 数据库快照警告\n\n无快照警告。"
    return "## 数据库快照警告\n\n" + "\n".join(f"- {_markdown_cell(warning)}" for warning in warning_list)


def render_database_snapshot(snapshot: Dict[str, Any]) -> str:
    """Render the structured database snapshot as isolated, readable tables."""
    safe_snapshot = make_json_serializable(snapshot) if isinstance(snapshot, Mapping) else {}
    return "\n\n".join(
        [
            f"快照时间：{_markdown_cell(safe_snapshot.get('timestamp'))}",
            _render_sqlite_snapshot(safe_snapshot.get("sqlite", {})),
            _render_long_term_snapshot(safe_snapshot.get("long_term_memory", [])),
            _render_midterm_snapshot(safe_snapshot.get("midterm_memory", {})),
            _render_entity_snapshot(safe_snapshot.get("entity_store", {})),
            _render_profile_snapshot(safe_snapshot.get("profile_view", {})),
            _render_snapshot_warnings(safe_snapshot.get("warnings", [])),
        ]
    )


@contextlib.contextmanager
def capture_retrieved_context(memory: Any, sink) -> Iterable[None]:
    """Capture the protected retrieval result while building answer messages end to end."""
    original_retrieve = memory._retrieve_context
    instance_attributes = getattr(memory, "__dict__", {})
    had_instance_override = "_retrieve_context" in instance_attributes
    previous_override = instance_attributes.get("_retrieve_context")

    def retrieve_and_capture(*args, **kwargs):
        context = original_retrieve(*args, **kwargs)
        sink(context)
        return context

    memory._retrieve_context = retrieve_and_capture
    try:
        yield
    finally:
        if had_instance_override:
            memory._retrieve_context = previous_override
        else:
            del memory._retrieve_context


def _calls_for_phase(calls: Iterable[Dict[str, Any]], phase: str) -> List[Dict[str, Any]]:
    return [call for call in calls if call.get("phase") == phase]


def _render_llm_calls(calls: Sequence[Dict[str, Any]]) -> str:
    if not calls:
        return "本阶段未调用 LLM"
    parts = []
    for call in calls:
        metadata = {
            "response_format": call.get("response_format"),
            "other_safe_kwargs": call.get("other_safe_kwargs"),
        }
        table_rows = [
            {"字段": "Phase", "内容": call.get("phase")},
            {"字段": "Source Type", "内容": call.get("source_type", "unknown")},
            {"字段": "Caller", "内容": _caller_label(call)},
            {"字段": "Module", "内容": call.get("caller_module", "unknown")},
            {"字段": "File", "内容": call.get("caller_file", "unknown")},
            {"字段": "Line", "内容": call.get("caller_line")},
            {"字段": "Provider", "内容": call.get("provider", "unknown")},
            {"字段": "Model", "内容": call.get("model", "unknown")},
            {"字段": "Timestamp", "内容": call.get("timestamp")},
        ]
        response = call.get("error") if call.get("error") is not None else call.get("raw_response")
        response_title = "#### 调用错误" if call.get("error") is not None else "#### 模型原始回答"
        parts.append(
            "\n\n".join(
                [
                    f"### LLM Call {call['call_index']}",
                    _render_markdown_table(table_rows, columns=("字段", "内容")),
                    f"#### 调用元数据\n\n```json\n{json_text(metadata)}\n```",
                    render_llm_messages(call.get("messages")),
                    f"{response_title}\n\n{_render_fenced_value(response)}",
                ]
            )
        )
    return "\n\n".join(parts)


def _render_layered_migration(migration: Dict[str, Any]) -> str:
    summary_rows = [
        {"字段": "本轮淘汰消息数量", "内容": migration.get("evicted_count", 0)},
        {"字段": "是否触发中期记忆", "内容": migration.get("midterm_triggered", False)},
        {"字段": "是否触发长期记忆", "内容": migration.get("long_term_triggered", False)},
    ]
    evicted_messages = _mapping_rows(migration.get("evicted_messages", []))
    message_columns = _ordered_columns(evicted_messages, ("role", "content", "name", "created_at"))
    return "\n\n".join(
        [
            _render_markdown_table(summary_rows, columns=("字段", "内容")),
            "### 本轮淘汰消息内容",
            _render_markdown_table(evicted_messages, columns=message_columns, empty_message="本轮没有淘汰消息。"),
        ]
    )


def _render_turn_markdown(record: Dict[str, Any]) -> str:
    info = record["turn"]
    separator = "\n\n============================================================\n\n"
    sections = [
        f"# Turn 基本信息\n\n```json\n{json_text(info)}\n```",
        f"## 1. 用户问题\n\n{info['query']}",
        "## 2. 数据集参考回答\n\n仅供查看，不参与模型输入。\n\n" + info["reference_answer"],
        f"## 3. 回答前数据库完整快照\n\n{render_database_snapshot(record.get('snapshot_before') or {})}",
        f"## 4. retrieve_context 输入参数\n\n```json\n{json_text(record.get('retrieve_input'))}\n```",
        f"## 5. retrieve_context 完整返回结果\n\n```json\n{json_text(record.get('context'))}\n```",
        "## 6. retrieve_context 阶段 LLM 调用\n\n" + _render_llm_calls(record["llm_calls"].get("retrieve_context", [])),
        f"## 7. 最终回答生成 Prompt\n\n{render_llm_messages(record.get('answer_prompt'))}",
        f"## 8. 最终回答模型原始输出\n\n{_render_fenced_value(record.get('answer_raw_response'))}",
        "## 最终回答阶段 LLM 调用详情\n\n" + _render_llm_calls(record["llm_calls"].get("answer_generation", [])),
        f"## 9. add() 输入\n\n```json\n{json_text(record.get('add_input'))}\n```",
        "## 10. add() 阶段 LLM 调用\n\n" + _render_llm_calls(record["llm_calls"].get("add", [])),
        f"## 11. add() 返回结果\n\n```json\n{json_text(record.get('add_result'))}\n```",
        f"## 本轮分层记忆迁移\n\n{_render_layered_migration(record.get('layered_migration') or {})}",
        f"## 12. 回答后数据库完整快照\n\n{render_database_snapshot(record.get('snapshot_after') or {})}",
        f"## 13. 本轮错误与耗时\n\n```json\n{json_text({'error': record.get('error'), 'elapsed_ms': record.get('elapsed_ms')})}\n```",
    ]
    return separator.join(sections) + "\n"


class RetrieveContextAddTraceRunner:
    """Execute and optionally persist the ten-turn traced workflow."""

    def __init__(
        self,
        *,
        memory: Any,
        turns: Sequence[Dict[str, Any]],
        selected_sessions: Sequence[Dict[str, Any]],
        user_id: str,
        recorder: TraceRecorder,
        test_mode: bool,
        output_root: Path,
        dataset_path: Path,
        memory_config_summary: Dict[str, Any],
        top_k: int = 20,
        threshold: float = 0.1,
        rerank: bool = False,
        continue_on_error: bool = False,
        snapshot_collector=collect_full_database_snapshot,
        run_timestamp: Optional[str] = None,
    ) -> None:
        self.memory = memory
        self.turns = list(turns)
        self.selected_sessions = list(selected_sessions)
        self.user_id = user_id
        self.recorder = recorder
        self.test_mode = test_mode
        self.output_root = Path(output_root)
        self.dataset_path = Path(dataset_path)
        self.memory_config_summary = make_json_serializable(memory_config_summary)
        self.top_k = top_k
        self.threshold = threshold
        self.rerank = rerank
        self.continue_on_error = continue_on_error
        self.snapshot_collector = snapshot_collector
        self.run_timestamp = run_timestamp or beijing_now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = self.output_root / self.run_timestamp if test_mode else None
        self.output_files: List[str] = []
        self.failed_turns: List[Dict[str, Any]] = []
        self.completed_turn_count = 0
        self.started_at = beijing_now_iso()

    def _snapshot(self) -> Dict[str, Any]:
        return self.snapshot_collector(self.memory, self.user_id)

    def _write(self, filename: str, content: str) -> None:
        if not self.test_mode or self.output_dir is None:
            return
        path = self.output_dir / filename
        atomic_write_text(path, content)
        self.output_files.append(filename)

    def _global_before_markdown(self, snapshot: Dict[str, Any]) -> str:
        config = {
            "selected_sessions": [session.get("session_id") for session in self.selected_sessions],
            "user_id": self.user_id,
            "dataset_path": str(self.dataset_path),
            "database_path": getattr(getattr(self.memory, "db", None), "db_path", None),
            "vector_collection": getattr(getattr(self.memory, "vector_store", None), "collection_name", None),
            "memory_config": self.memory_config_summary,
        }
        return (
            f"# 任务开始前数据库快照\n\n## 任务配置\n\n```json\n{json_text(config)}\n```\n\n"
            f"{render_database_snapshot(snapshot)}\n"
        )

    def _global_after_markdown(self, snapshot: Dict[str, Any]) -> str:
        summary = {
            "completed_turn_count": self.completed_turn_count,
            "failed_turn_count": len(self.failed_turns),
            "failed_turns": self.failed_turns,
        }
        return (
            f"# 任务结束后数据库快照\n\n## 运行结果摘要\n\n```json\n{json_text(summary)}\n```\n\n"
            f"{render_database_snapshot(snapshot)}\n"
        )

    def _run_turn(self, turn: Dict[str, Any]) -> None:
        index = turn["global_turn_index"]
        started = time.perf_counter()
        call_start = len(self.recorder.calls)
        eviction_start = len(self.recorder.evictions)
        record: Dict[str, Any] = {
            "turn": turn,
            "snapshot_before": None,
            "retrieve_input": None,
            "context": None,
            "answer_prompt": None,
            "answer_raw_response": None,
            "generated_answer": None,
            "add_input": None,
            "add_result": None,
            "snapshot_after": None,
            "llm_calls": {},
            "layered_migration": None,
            "error": None,
            "elapsed_ms": None,
        }
        error: Optional[Exception] = None

        with self.recorder.turn(index):
            try:
                if self.test_mode:
                    record["snapshot_before"] = self._snapshot()
                    self.recorder.emit_database_snapshot(
                        f"[TURN {index:02d}][DATABASE BEFORE]", record["snapshot_before"]
                    )

                retrieve_input = {
                    "query": turn["query"],
                    "user_id": turn["user_id"],
                    "session_id": turn["session_id"],
                    "top_k": self.top_k,
                    "threshold": self.threshold,
                    "rerank": self.rerank,
                    "explain": True,
                }
                record["retrieve_input"] = retrieve_input
                phase_call_start = len(self.recorder.calls)
                context_capture = (
                    capture_retrieved_context(
                        self.memory,
                        lambda context: record.__setitem__("context", context),
                    )
                    if self.test_mode
                    else contextlib.nullcontext()
                )
                with self.recorder.phase("retrieve_context"), context_capture:
                    record["answer_prompt"] = self.memory.build_agent_answer_messages(**retrieve_input)
                self.recorder.emit_no_llm_calls("retrieve_context", phase_call_start)
                if record["context"] is not None:
                    self.recorder.emit(f"[TURN {index:02d}][RETRIEVED CONTEXT]", record["context"])
                self.recorder.emit_llm_messages(
                    f"[TURN {index:02d}][ANSWER PROMPT]",
                    record["answer_prompt"],
                )

                phase_call_start = len(self.recorder.calls)
                with self.recorder.phase("answer_generation"):
                    raw_answer = self.memory.llm.generate_response(messages=record["answer_prompt"])
                self.recorder.emit_no_llm_calls("answer_generation", phase_call_start)
                record["answer_raw_response"] = raw_answer
                record["generated_answer"] = normalize_llm_response(raw_answer)
                self.recorder.emit_llm_response(f"[TURN {index:02d}][ANSWER RESPONSE]", raw_answer)

                messages = [
                    {"role": "user", "content": turn["query"]},
                    {"role": "assistant", "content": record["generated_answer"]},
                ]
                record["add_input"] = {
                    "messages": messages,
                    "user_id": turn["user_id"],
                    "run_id": turn["session_id"],
                    "infer": True,
                }
                self.recorder.emit(f"[TURN {index:02d}][ADD INPUT]", record["add_input"])
                phase_call_start = len(self.recorder.calls)
                with self.recorder.phase("add"):
                    record["add_result"] = self.memory.add(**record["add_input"])
                self.recorder.emit_no_llm_calls("add", phase_call_start)
                self.recorder.emit(f"[TURN {index:02d}][ADD RESULT]", record["add_result"])

                if self.test_mode:
                    record["snapshot_after"] = self._snapshot()
                    self.recorder.emit_database_snapshot(
                        f"[TURN {index:02d}][DATABASE AFTER]", record["snapshot_after"]
                    )
                self.completed_turn_count += 1
            except Exception as exc:
                error = exc
                record["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
                self.failed_turns.append({"global_turn_index": index, **record["error"]})
                if self.test_mode and record["snapshot_after"] is None:
                    try:
                        record["snapshot_after"] = self._snapshot()
                    except Exception as snapshot_exc:
                        record["error"]["snapshot_after_error"] = f"{type(snapshot_exc).__name__}: {snapshot_exc}"
            finally:
                turn_calls = self.recorder.calls[call_start:]
                turn_evictions = self.recorder.evictions[eviction_start:]
                record["llm_calls"] = {
                    phase: _calls_for_phase(turn_calls, phase)
                    for phase in ("retrieve_context", "answer_generation", "add")
                }
                record["layered_migration"] = (
                    turn_evictions[-1]
                    if turn_evictions
                    else {
                        "evicted_count": 0,
                        "evicted_messages": [],
                        "midterm_triggered": False,
                        "long_term_triggered": False,
                    }
                )
                record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
                if self.test_mode:
                    filename = f"{index:02d}_session{turn['session_order']}_turn{turn['session_turn_index']}.md"
                    self._write(filename, _render_turn_markdown(record))

        if error is not None and not self.continue_on_error:
            raise error

    def _manifest(self, finished_at: str) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": finished_at,
            "test_mode": self.test_mode,
            "dataset_path": str(self.dataset_path),
            "selected_session_indexes": list(SELECTED_SESSION_INDEXES),
            "selected_session_ids": [session.get("session_id") for session in self.selected_sessions],
            "user_id": self.user_id,
            "expected_turn_count": EXPECTED_TURN_COUNT,
            "completed_turn_count": self.completed_turn_count,
            "failed_turns": self.failed_turns,
            "output_files": self.output_files,
            "memory_config_summary": self.memory_config_summary,
        }

    def run(self) -> Dict[str, Any]:
        if self.test_mode and self.output_dir is not None:
            candidate = self.output_dir
            suffix = 1
            while candidate.exists():
                candidate = self.output_root / f"{self.run_timestamp}_{suffix:02d}"
                suffix += 1
            self.output_dir = candidate
            self.output_dir.mkdir(parents=True, exist_ok=False)

        pending_error: Optional[Exception] = None
        try:
            if self.test_mode:
                initial_snapshot = self._snapshot()
                self.recorder.emit_database_snapshot("[GLOBAL][DATABASE BEFORE]", initial_snapshot)
                self._write("00_initial_database_snapshot.md", self._global_before_markdown(initial_snapshot))

            for turn in self.turns:
                try:
                    self._run_turn(turn)
                except Exception as exc:
                    pending_error = exc
                    break
        finally:
            finished_at = beijing_now_iso()
            if self.test_mode:
                final_snapshot = self._snapshot()
                self.recorder.emit_database_snapshot("[GLOBAL][DATABASE AFTER]", final_snapshot)
                self._write("11_final_database_snapshot.md", self._global_after_markdown(final_snapshot))
                manifest = self._manifest(finished_at)
                atomic_write_json(self.output_dir / "run_manifest.json", manifest)
            else:
                manifest = self._manifest(finished_at)

        if pending_error is not None:
            raise pending_error
        return manifest


def build_memory(args: argparse.Namespace):
    """Reuse the existing financial test Memory initialization and enable profile updates."""
    args.state_root.mkdir(parents=True, exist_ok=True)
    memory, config = build_base_memory(args, args.state_root)
    memory.config.profile.enabled = True
    memory.config.profile.update_on_add = True
    config["profile"] = {"enabled": True, "update_on_add": True}
    return memory, config


def reset_test_databases(memory: Any) -> None:
    """Clear every database owned by the configured test Memory instance."""
    entity_store_descriptor = getattr(type(memory), "entity_store", None)
    if isinstance(entity_store_descriptor, property):
        _ = memory.entity_store  # Initialize it so Memory.reset() also clears stale entity records.

    reset = getattr(memory, "reset", None)
    if not callable(reset):
        raise RuntimeError("The configured Memory instance does not support reset()")
    reset()


def _config_value(config: Any, name: str) -> Any:
    if config is None:
        return None
    if isinstance(config, Mapping):
        return config.get(name)
    value = getattr(config, name, None)
    if value is not None:
        return value
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(name)
        except (KeyError, TypeError):
            return None
    return None


def _first_identity_value(candidates: Iterable[Any]) -> str:
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate)
    return "unknown"


def resolve_llm_identity(memory: Any, llm: Any) -> tuple[str, str]:
    """Resolve provider/model from object or dictionary configs without reading credentials."""
    memory_config = _config_value(memory, "config")
    llm_config = _config_value(memory_config, "llm")
    provider_config = _config_value(llm_config, "config")
    wrapped = getattr(llm, "_wrapped", llm)
    wrapped_config = _config_value(wrapped, "config")

    provider = _first_identity_value(
        (
            _config_value(llm_config, "provider"),
            _config_value(provider_config, "provider"),
            _config_value(wrapped, "provider"),
        )
    )
    model = _first_identity_value(
        (
            _config_value(provider_config, "model"),
            _config_value(provider_config, "model_name"),
            _config_value(llm_config, "model"),
            _config_value(llm_config, "model_name"),
            _config_value(wrapped_config, "model"),
            _config_value(wrapped_config, "model_name"),
            _config_value(wrapped, "model"),
            _config_value(wrapped, "model_name"),
        )
    )
    return provider, model


def _llm_identity(memory: Any) -> tuple[str, str]:
    """Backward-compatible wrapper for existing callers."""
    return resolve_llm_identity(memory, memory.llm)


def close_memory(memory: Any) -> None:
    close = getattr(memory, "close", None)
    if callable(close):
        close()
    client_close = getattr(getattr(getattr(memory, "vector_store", None), "client", None), "close", None)
    if callable(client_close):
        client_close()


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    args.data = args.data.resolve()
    args.output_root = args.output_root.resolve()
    args.state_root = args.state_root.resolve()
    test_mode = resolve_test_mode(args.test_mode)
    selected_sessions, turns, user_id = load_selected_turns(args.data, user_id=args.user_id)

    memory = None
    try:
        memory, config = build_memory(args)
        reset_test_databases(memory)
        recorder = TraceRecorder(test_mode)
        if test_mode:
            provider, model = resolve_llm_identity(memory, memory.llm)
            memory.llm = TracedLLM(memory.llm, recorder, provider=provider, model=model)
            install_test_eviction_trace(memory, recorder)

        runner = RetrieveContextAddTraceRunner(
            memory=memory,
            turns=turns,
            selected_sessions=selected_sessions,
            user_id=user_id,
            recorder=recorder,
            test_mode=test_mode,
            output_root=args.output_root,
            dataset_path=args.data,
            memory_config_summary=config,
            top_k=args.top_k,
            threshold=args.threshold,
            rerank=args.rerank,
            continue_on_error=args.continue_on_error,
        )
        manifest = runner.run()
        if test_mode:
            print(f"\n[GLOBAL][OUTPUT DIRECTORY]\n{runner.output_dir}")
            print(f"[GLOBAL][COMPLETED TURNS]\n{manifest['completed_turn_count']}")
    finally:
        if memory is not None:
            close_memory(memory)


if __name__ == "__main__":
    main(sys.argv[1:])

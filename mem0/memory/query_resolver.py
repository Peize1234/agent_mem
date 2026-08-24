"""Conservative ShortTerm-aware query resolution shared by production and tuning."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Mapping, Sequence

from mem0.configs.query_prompts import QUERY_REFERENCE_RESOLUTION_PROMPT
from mem0.memory.utils import extract_json, remove_code_blocks

logger = logging.getLogger(__name__)


def visible_query_history(messages: Sequence[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    """Project visible ShortTerm messages to the resolver's stable history contract."""
    history = []
    for message in messages or []:
        role = str(message.get("role") or "").strip()
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
            continue
        history.append({"role": role, "content": content})
    return history


def build_query_resolution_messages(
    query: str,
    visible_messages: Sequence[Mapping[str, Any]],
    *,
    prompt: str = QUERY_REFERENCE_RESOLUTION_PROMPT,
) -> list[dict[str, str]]:
    """Build the exact production resolver request used by tuner candidates as well."""
    payload = {
        "current_query": query,
        "recent_history": visible_query_history(visible_messages),
    }
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def parse_resolved_query(value: Any, original_query: str) -> str:
    """Parse one strict resolver response and enforce a conservative length guard."""
    parsed: Mapping[str, Any] = {}
    if isinstance(value, Mapping):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed_value = json.loads(remove_code_blocks(value), strict=False)
        except (TypeError, ValueError, json.JSONDecodeError):
            try:
                parsed_value = json.loads(extract_json(value), strict=False)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed_value = {}
        if isinstance(parsed_value, Mapping):
            parsed = parsed_value

    resolved = parsed.get("resolved_query")
    if not isinstance(resolved, str) or not resolved.strip():
        raise ValueError("query resolution response has no resolved_query")
    resolved = resolved.strip()
    if len(resolved) > max(2000, len(original_query) * 8):
        raise ValueError("resolved_query exceeded the conservative length guard")
    return resolved


class QueryResolver:
    """Resolve a query against visible ShortTerm with fail-closed original fallback."""

    def __init__(
        self,
        llm: Any,
        *,
        prompt: str = QUERY_REFERENCE_RESOLUTION_PROMPT,
        request_options: Mapping[str, Any] | None = None,
    ):
        self.llm = llm
        self.prompt = prompt
        self.request_options = dict(request_options or {})
        conflicts = sorted({"messages", "response_format"} & set(self.request_options))
        if conflicts:
            raise ValueError(f"query resolver request options cannot override: {', '.join(conflicts)}")

    def resolve(self, query: str, visible_messages: Sequence[Mapping[str, Any]] | None) -> str:
        history = visible_query_history(visible_messages)
        if not history:
            return query
        try:
            response = self.llm.generate_response(
                messages=build_query_resolution_messages(query, history, prompt=self.prompt),
                response_format={"type": "json_object"},
                **self.request_options,
            )
            return parse_resolved_query(response, query)
        except Exception as exc:
            logger.warning("Query reference resolution failed; using original query: %s", exc)
            return query

    async def resolve_async(
        self,
        query: str,
        visible_messages: Sequence[Mapping[str, Any]] | None,
    ) -> str:
        history = visible_query_history(visible_messages)
        if not history:
            return query
        request = {
            "messages": build_query_resolution_messages(query, history, prompt=self.prompt),
            "response_format": {"type": "json_object"},
            **self.request_options,
        }
        try:
            async_generate = getattr(self.llm, "generate_response_async", None)
            if inspect.iscoroutinefunction(async_generate):
                response = await async_generate(**request)
            else:
                response = await asyncio.to_thread(lambda: self.llm.generate_response(**request))
            return parse_resolved_query(response, query)
        except Exception as exc:
            logger.warning("Async query reference resolution failed; using original query: %s", exc)
            return query

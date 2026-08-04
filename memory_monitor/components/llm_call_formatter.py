from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence


_ROLE_LABELS = {
    "system": "System",
    "developer": "Developer",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
    "function": "Function",
}
_NO_CONTENT = "（无文本内容）"
_NO_ANSWER = "（模型未返回文本回答）"
_FAILED_ANSWER = "（调用失败，未返回文本回答）"
_TOOL_BLOCK_TYPES = {"function_call", "tool_call", "tool_use"}
_JSON_FENCE = re.compile(
    r"\A```json[ \t]*(?:\r?\n)(?P<body>[\s\S]*?)(?:\r?\n)```[ \t]*\Z",
    re.IGNORECASE,
)
_FENCED_CODE_START = re.compile(r"^ {0,3}(?:(?P<backticks>`{3,})[^`\r\n]*|(?P<tildes>~{3,})[^\r\n]*)(?:\r?\n|$)")
_JSON_START_BOUNDARY = frozenset("><:=,;(-+*/|&'\"，。；：！？、（【")
_JSON_END_BOUNDARY = frozenset("<>.,;:!?)'\"，。；：！？、）】")
_JSON_CONTAINER_STARTS = {
    "{": frozenset(('"', "}")),
    "[": frozenset('[{"-0123456789tfn]'),
}
_DEBUG_RESPONSE_FIELDS = {
    "arguments",
    "created",
    "finish_reason",
    "function_call",
    "functions",
    "id",
    "input_tokens",
    "metadata",
    "model",
    "output_tokens",
    "parameters",
    "system_fingerprint",
    "token_usage",
    "tool_calls",
    "tool_choice",
    "tools",
    "usage",
}


@dataclass(frozen=True)
class PromptMessage:
    role: str
    content: str


@dataclass(frozen=True)
class CallTextPart:
    content: str
    is_json: bool = False


def format_prompt_messages(messages: Any) -> tuple[PromptMessage, ...]:
    """Convert persisted model messages into readable role/content sections."""
    if isinstance(messages, Mapping):
        messages = [messages]
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        return (PromptMessage("Prompt", _format_content(messages)),)
    if not messages:
        return (PromptMessage("Prompt", _NO_CONTENT),)

    formatted = []
    for message in messages:
        if isinstance(message, Mapping):
            raw_role = str(message.get("role") or "message").strip().lower()
            role = _ROLE_LABELS.get(raw_role, raw_role.replace("_", " ").title() or "Message")
            if raw_role == "tool" and message.get("name"):
                role = f"{role} · {message['name']}"
            content = _format_message_content(message)
        else:
            role = "Message"
            content = _format_content(message)
        formatted.append(PromptMessage(role, content))
    return tuple(formatted)


def _format_message_content(message: Mapping[str, Any]) -> str:
    parts = []
    if message.get("content") is not None:
        parts.append(_format_content(message["content"]))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes, bytearray)):
        parts.extend(_format_tool_call(call) for call in tool_calls)
    return "\n\n".join(part for part in parts if part.strip()) or _NO_CONTENT


def _format_tool_call(call: Any) -> str:
    if not isinstance(call, Mapping):
        return f"Tool Call\n\n{_format_content(call)}"
    function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
    name = call.get("name") or function.get("name") or "unknown"
    arguments = call.get("arguments", function.get("arguments"))
    details = [f"Tool Call · {name}"]
    if call.get("id"):
        details.append(f"Call ID: {call['id']}")
    if arguments is not None:
        details.append(f"Arguments:\n{_format_content(arguments)}")
    return "\n\n".join(details)


def format_model_answer(call: Mapping[str, Any]) -> str:
    answer = extract_response_text(call.get("response"))
    if answer == _NO_ANSWER and call.get("status") == "failed":
        return _FAILED_ANSWER
    return answer


def extract_response_text(response: Any) -> str:
    """Extract provider-independent answer text, with a safe structured fallback."""
    if isinstance(response, Sequence) and not isinstance(response, (str, bytes, bytearray)):
        if not _is_provider_text_parts(response):
            return _format_json_value(response)

    extracted = _extract_text(response)
    if extracted and extracted.strip():
        return _format_json_response_text(extracted)

    fallback = _safe_structured_fallback(response)
    return _format_json_response_text(fallback) if fallback else _NO_ANSWER


def is_json_document(value: str) -> bool:
    """Return whether *value* is a complete JSON object or array."""
    return _parse_json_document(value, allow_fence=False) is not None


def format_json_document(value: str) -> str | None:
    """Pretty-print a complete JSON object or array without changing *value*."""
    parsed = _parse_json_document(value, allow_fence=False)
    return _format_json_value(parsed) if parsed is not None else None


def split_mixed_text_and_json(content: str) -> tuple[CallTextPart, ...]:
    """Split Markdown text around complete JSON objects/arrays outside code fences."""
    parts: list[CallTextPart] = []
    for region, can_contain_json in _markdown_regions(content):
        if can_contain_json:
            _split_json_region(region, parts)
        else:
            _append_call_text_part(parts, region, is_json=False)
    return tuple(parts) or (CallTextPart(content),)


def _format_json_response_text(value: str) -> str:
    """Pretty-print only complete object/array answers, including JSON fences."""
    stripped = value.strip()
    parsed = _parse_json_document(stripped, allow_fence=True)
    return _format_json_value(parsed) if parsed is not None else stripped


def _parse_json_document(value: str, *, allow_fence: bool) -> dict[str, Any] | list[Any] | None:
    candidate = value.strip()
    if allow_fence:
        fence = _JSON_FENCE.fullmatch(candidate)
        if fence is not None:
            candidate = fence.group("body").strip()
    try:
        parsed = json.loads(candidate)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _format_json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _markdown_regions(content: str) -> Iterator[tuple[str, bool]]:
    """Yield text regions with whether embedded JSON detection is allowed."""
    lines = content.splitlines(keepends=True)
    cursor = 0
    line_index = 0
    line_start = 0

    while line_index < len(lines):
        line = lines[line_index]
        match = _FENCED_CODE_START.match(line)
        if match is None:
            line_start += len(line)
            line_index += 1
            continue

        if cursor < line_start:
            yield content[cursor:line_start], True

        fence = match.group("backticks") or match.group("tildes")
        closing_fence = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*(?:\r?\n|$)")
        block_end = len(content)
        search_index = line_index + 1
        search_start = line_start + len(line)
        while search_index < len(lines):
            closing_line = lines[search_index]
            search_start += len(closing_line)
            if closing_fence.fullmatch(closing_line):
                block_end = search_start
                search_index += 1
                break
            search_index += 1

        yield content[line_start:block_end], False
        cursor = block_end
        line_start = block_end
        line_index = search_index

    if cursor < len(content):
        yield content[cursor:], True


def _split_json_region(content: str, parts: list[CallTextPart]) -> None:
    decoder = json.JSONDecoder()
    cursor = 0
    plain_start = 0

    while cursor < len(content):
        if (
            content[cursor] not in "{["
            or not _has_json_start_boundary(content, cursor)
            or not _looks_like_json_container(content, cursor)
        ):
            cursor += 1
            continue

        try:
            parsed, end = decoder.raw_decode(content, cursor)
        except json.JSONDecodeError:
            parsed = None
            end = None

        if isinstance(parsed, (dict, list)) and end is not None and _has_json_end_boundary(content, end):
            _append_call_text_part(parts, content[plain_start:cursor], is_json=False)
            _append_call_text_part(parts, content[cursor:end], is_json=True)
            cursor = end
            plain_start = end
            continue

        container_end = _json_like_container_end(content, cursor)
        if container_end is not None:
            cursor = container_end
        else:
            # An incomplete outer document must stay plain; do not promote a
            # valid nested object or array from inside the malformed text.
            cursor = len(content)

    _append_call_text_part(parts, content[plain_start:], is_json=False)


def _append_call_text_part(parts: list[CallTextPart], content: str, *, is_json: bool) -> None:
    if not content:
        return
    if parts and not is_json and not parts[-1].is_json:
        parts[-1] = CallTextPart(parts[-1].content + content)
        return
    parts.append(CallTextPart(content, is_json=is_json))


def _has_json_start_boundary(content: str, start: int) -> bool:
    if start == 0:
        return True
    previous = content[start - 1]
    return previous.isspace() or previous in _JSON_START_BOUNDARY


def _has_json_end_boundary(content: str, end: int) -> bool:
    if end == len(content):
        return True
    following = content[end]
    return following.isspace() or following in _JSON_END_BOUNDARY


def _looks_like_json_container(content: str, start: int) -> bool:
    cursor = start + 1
    while cursor < len(content) and content[cursor].isspace():
        cursor += 1
    return cursor == len(content) or content[cursor] in _JSON_CONTAINER_STARTS[content[start]]


def _json_like_container_end(content: str, start: int) -> int | None:
    """Find a balanced candidate end while respecting JSON string escaping."""
    closing_for = {"{": "}", "[": "]"}
    stack = [content[start]]
    in_string = False
    escaped = False

    for cursor in range(start + 1, len(content)):
        character = content[cursor]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in closing_for:
            stack.append(character)
        elif character in "}]":
            if character != closing_for[stack[-1]]:
                return cursor + 1
            stack.pop()
            if not stack:
                return cursor + 1
    return None


def _is_provider_text_parts(value: Sequence[Any]) -> bool:
    """Keep provider text-block arrays on the normal text extraction path."""
    if not value:
        return False
    return all(
        isinstance(item, Mapping)
        and (
            str(item.get("type") or "").lower() in _TOOL_BLOCK_TYPES
            or any(field in item for field in ("text", "content", "output_text"))
        )
        for item in value
    )


def _format_content(value: Any) -> str:
    if value is None:
        return _NO_CONTENT
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = [_format_content_part(item) for item in value]
        return "\n\n".join(part for part in parts if part.strip()) or _NO_CONTENT
    if isinstance(value, Mapping):
        return _format_content_part(value)
    return str(value)


def _format_content_part(value: Any) -> str:
    if not isinstance(value, Mapping):
        return _format_content(value)
    if str(value.get("type") or "").lower() in _TOOL_BLOCK_TYPES:
        return _NO_CONTENT
    for field in ("text", "content"):
        if field in value and value[field] is not None:
            return _format_content(value[field])
    image = value.get("image_url") or value.get("url")
    if image:
        if isinstance(image, Mapping):
            image = image.get("url") or image
        return f"[Image] {image}"
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _extract_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = [_extract_text(item) for item in value]
        text_parts = [part for part in parts if part and part.strip()]
        return "\n\n".join(text_parts) if text_parts else None
    if not isinstance(value, Mapping):
        return None

    for field in ("content", "text", "answer", "output_text"):
        if field in value:
            text = _extract_text(value[field])
            if text and text.strip():
                return text
    for field in ("message", "choices", "output", "response", "result"):
        if field in value:
            text = _extract_text(value[field])
            if text and text.strip():
                return text
    return None


def _safe_structured_fallback(value: Any) -> str | None:
    sanitized = _sanitize_fallback(value)
    if sanitized in (None, {}, []):
        return None
    if isinstance(sanitized, str):
        return sanitized
    return _format_json_value(sanitized)


def _sanitize_fallback(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        if str(value.get("type") or "").lower() in _TOOL_BLOCK_TYPES:
            return None
        sanitized = {
            str(key): _sanitize_fallback(item) for key, item in value.items() if str(key) not in _DEBUG_RESPONSE_FIELDS
        }
        return {key: item for key, item in sanitized.items() if item not in (None, {}, [])}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sanitized = [_sanitize_fallback(item) for item in value]
        return [item for item in sanitized if item not in (None, {}, [])]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

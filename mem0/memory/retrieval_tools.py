from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from mem0.configs.base import AgenticRetrievalConfig

logger = logging.getLogger(__name__)

_MAX_AGENTIC_QUERIES = int(AgenticRetrievalConfig.model_json_schema()["properties"]["max_queries"]["maximum"])

SEARCH_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": (
            "在当前用户和当前会话作用域内检索中期记忆，用于补充最终回答模型缺失的历史上下文。"
            "工具返回结果不是用户问题的答案；调用后只能筛选并整理与当前上下文缺口直接相关的历史信息。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                    "minItems": 1,
                    "maxItems": _MAX_AGENTIC_QUERIES,
                    "description": (
                        f"1到{_MAX_AGENTIC_QUERIES}个互补的中期记忆检索词；"
                        "尽量包含分析主体、报告期间、分析任务、指标口径、"
                        "数据场景或版本，以及需要恢复的具体历史信息"
                    ),
                }
            },
            "required": ["queries"],
            "additionalProperties": False,
        },
    },
}

MEMORY_TOOLS = [SEARCH_MEMORY_TOOL]

QueryText = Annotated[str, Field(min_length=1, max_length=500)]


class SearchMemoryArguments(BaseModel):
    model_config = {"extra": "forbid"}

    queries: list[QueryText] = Field(min_length=1, max_length=_MAX_AGENTIC_QUERIES)

    @field_validator("queries")
    @classmethod
    def normalize_queries(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            query = value.strip()
            if not query:
                raise ValueError("queries must not contain blank values")
            if query not in normalized:
                normalized.append(query)
        return normalized


def serialize_tool_result(result: dict[str, Any]) -> str:
    """Serialize a tool payload in a stable, readable, JSON-safe form."""
    return json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _tool_error(error: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message}


def _validation_error(exc: ValidationError) -> dict[str, Any]:
    fields = sorted({".".join(str(part) for part in item["loc"]) for item in exc.errors(include_url=False)})
    result = _tool_error("InvalidArguments", "工具参数无效")
    result["fields"] = fields
    return result


def _score(item: dict[str, Any]) -> float:
    try:
        return float(item.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


class MemoryToolExecutor:
    """Run one scoped, multi-query mid-term page search for one answer."""

    def __init__(
        self,
        memory: Any,
        *,
        user_id: str,
        run_id: str,
        config: AgenticRetrievalConfig,
        record_midterm_visits: bool = True,
        exclude_midterm_page_ids: set[str] | None = None,
    ) -> None:
        self.memory = memory
        self.user_id = user_id
        self.run_id = run_id
        self.config = config
        self.record_midterm_visits = record_midterm_visits
        self.exclude_midterm_page_ids = {str(value) for value in (exclude_midterm_page_ids or set())}
        self._pending_valid_page_ids: list[str] = []
        self._scope_filters = {"user_id": user_id, "run_id": run_id}

    def execute(self, name: str, arguments: Any) -> dict[str, Any]:
        if name != "search_memory":
            return _tool_error("UnknownTool", "未知的记忆工具")
        return self.search_memory(arguments)

    def search_memory(self, arguments: Any) -> dict[str, Any]:
        parsed, error = self._parse_arguments(arguments)
        if error is not None:
            return error
        if not self.memory._midterm_enabled():
            return {"ok": True, "items": []}

        results_by_query: list[list[dict[str, Any]]] = []
        errors: list[dict[str, str]] = []
        if len(parsed.queries) == 1:
            try:
                results_by_query.append(self._search_query(parsed.queries[0]))
            except Exception as exc:
                self._record_retrieval_error(exc, errors)
        else:
            with ThreadPoolExecutor(max_workers=len(parsed.queries)) as pool:
                futures = [pool.submit(self._search_query, query) for query in parsed.queries]
                for future in futures:
                    try:
                        results_by_query.append(future.result())
                    except Exception as exc:
                        self._record_retrieval_error(exc, errors)

        return self._finalize_results(
            results_by_query,
            errors,
            successful_queries=len(parsed.queries) - len(errors),
        )

    def _parse_arguments(
        self,
        arguments: Any,
    ) -> tuple[SearchMemoryArguments | None, dict[str, Any] | None]:
        try:
            parsed = SearchMemoryArguments.model_validate(arguments)
        except ValidationError as exc:
            return None, _validation_error(exc)
        if len(parsed.queries) > self.config.max_queries:
            return None, {
                **_tool_error("InvalidArguments", "工具参数无效"),
                "fields": ["queries"],
            }
        return parsed, None

    def _search_query(self, query: str) -> list[dict[str, Any]]:
        return self.memory.midterm_retriever.search(
            query,
            dict(self._scope_filters),
            record_visits=False,
            candidate_pool_size=self.config.candidate_pool_size,
        )

    def _finalize_results(
        self,
        results_by_query: list[list[dict[str, Any]]],
        errors: list[dict[str, str]],
        *,
        successful_queries: int,
    ) -> dict[str, Any]:
        return self._build_payload(
            results_by_query,
            errors,
            successful_queries=successful_queries,
        )

    def _page_ids_for_valid_recall(self, items: list[dict[str, Any]]) -> list[str]:
        if not self.record_midterm_visits:
            return []
        page_ids = []
        for item in items:
            result_id = str(item.get("result_id") or "")
            if not result_id.startswith("mid_term_page:"):
                continue
            page_id = result_id.removeprefix("mid_term_page:")
            if page_id and page_id not in self.exclude_midterm_page_ids:
                page_ids.append(page_id)
        return page_ids

    def confirm_last_results(self) -> None:
        """Confirm the pending tool result immediately before it enters model context."""
        page_ids = list(self._pending_valid_page_ids)
        self._pending_valid_page_ids = []
        if not page_ids:
            return
        try:
            current_turn_index = self.memory.midterm_memory.current_turn_index(self._scope_filters)
            confirm = getattr(self.memory, "_confirm_valid_midterm_page_ids", None)
            if callable(confirm):
                confirm(page_ids, current_turn_index=current_turn_index)
            else:
                self.memory.midterm_memory.record_valid_recalls(
                    page_ids,
                    recall_turn_index=current_turn_index,
                )
        except Exception:
            logger.exception(
                "Failed to record agentic valid mid-term recalls user_id=%s run_id=%s",
                self.user_id,
                self.run_id,
            )

    def _record_retrieval_error(self, exc: Exception, errors: list[dict[str, str]]) -> None:
        logger.error(
            "Agentic mid-term retrieval failed for user_id=%s run_id=%s",
            self.user_id,
            self.run_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        errors.append(
            {
                "error": type(exc).__name__,
                "message": "记忆检索暂时不可用",
            }
        )

    def _build_payload(
        self,
        results_by_query: list[list[dict[str, Any]]],
        errors: list[dict[str, str]],
        *,
        successful_queries: int,
    ) -> dict[str, Any]:
        best_by_page_id: dict[str, dict[str, Any]] = {}
        for raw_results in results_by_query:
            session_summaries = self._session_summaries(raw_results)
            for raw_item in raw_results:
                if raw_item.get("source") != "mid_term_page":
                    continue
                item = self._normalize_page(raw_item, session_summaries)
                result_id = item["result_id"]
                current = best_by_page_id.get(result_id)
                if current is None or _score(item) > _score(current):
                    best_by_page_id[result_id] = item

        items = sorted(best_by_page_id.values(), key=_score, reverse=True)[: self.config.max_total_results]
        payload: dict[str, Any] = {"ok": successful_queries > 0, "items": items}
        if errors:
            payload["errors"] = errors
        if successful_queries == 0:
            payload.update(error="RetrievalUnavailable", message="记忆检索暂时不可用")
        fitted = self._fit_payload(payload)
        self._pending_valid_page_ids = (
            self._page_ids_for_valid_recall(fitted.get("items") or []) if fitted.get("ok") is True else []
        )
        return fitted

    @staticmethod
    def _session_summaries(raw_results: list[dict[str, Any]]) -> dict[str, str]:
        summaries = {}
        for item in raw_results:
            if item.get("source") != "mid_term_session":
                continue
            session_id = item.get("session_id") or item.get("id")
            summary = str(item.get("summary") or item.get("memory") or "")
            if session_id not in (None, "") and summary:
                summaries[str(session_id)] = summary
        return summaries

    def _normalize_page(
        self,
        raw_item: dict[str, Any],
        session_summaries: dict[str, str],
    ) -> dict[str, Any]:
        content = str(raw_item.get("raw_dialogue") or raw_item.get("summary") or raw_item.get("memory") or "")
        summary = str(raw_item.get("summary") or raw_item.get("memory") or "")
        session_id = raw_item.get("session_id")
        session_summary = str(raw_item.get("session_summary") or session_summaries.get(str(session_id), ""))
        record_id = raw_item.get("id")
        if record_id in (None, "") or len(str(record_id)) > 200:
            stable_text = f"{session_id or ''}\0{summary}\0{content}"
            record_id = hashlib.sha256(stable_text.encode("utf-8")).hexdigest()[:32]

        item = {
            "result_id": f"mid_term_page:{record_id}",
            "score": raw_item.get("score"),
            "created_at": raw_item.get("created_at"),
            "session_summary": session_summary,
            "content": content,
        }
        if summary and summary != content:
            item["summary"] = summary
        return item

    def _fit_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if len(serialize_tool_result(payload)) <= self.config.max_tool_result_chars:
            return payload

        items = list(payload.get("items") or [])
        fitted = {**payload, "items": items}
        while len(items) > 1 and len(serialize_tool_result(fitted)) > self.config.max_tool_result_chars:
            items.pop()

        if len(serialize_tool_result(fitted)) <= self.config.max_tool_result_chars:
            return fitted
        return _tool_error("ToolResultTooLarge", "单条完整中期记忆超过工具消息长度限制")


class AsyncMemoryToolExecutor(MemoryToolExecutor):
    """Async counterpart with parallel query execution off the event loop."""

    async def execute(self, name: str, arguments: Any) -> dict[str, Any]:
        if name != "search_memory":
            return _tool_error("UnknownTool", "未知的记忆工具")
        return await self.search_memory(arguments)

    async def search_memory(self, arguments: Any) -> dict[str, Any]:
        parsed, error = self._parse_arguments(arguments)
        if error is not None:
            return error
        if not self.memory._midterm_enabled():
            return {"ok": True, "items": []}

        pool = ThreadPoolExecutor(max_workers=len(parsed.queries))
        futures = [pool.submit(self._search_query, query) for query in parsed.queries]
        completed = False
        try:
            while not all(future.done() for future in futures):
                await asyncio.sleep(0.001)

            results_by_query: list[list[dict[str, Any]]] = []
            errors: list[dict[str, str]] = []
            for future in futures:
                try:
                    results_by_query.append(future.result())
                except Exception as exc:
                    self._record_retrieval_error(exc, errors)

            finalizer = pool.submit(
                self._finalize_results,
                results_by_query,
                errors,
                successful_queries=len(parsed.queries) - len(errors),
            )
            while not finalizer.done():
                await asyncio.sleep(0.001)
            completed = True
            return finalizer.result()
        finally:
            pool.shutdown(wait=completed, cancel_futures=not completed)

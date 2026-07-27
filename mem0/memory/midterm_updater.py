import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT
from mem0.memory.midterm import compute_session_heat, keyword_overlap
from mem0.memory.observability import NoOpObservationSink, ObservationContext, emit_safely, observation_stage
from mem0.memory.utils import extract_json, remove_code_blocks
from mem0.utils.timestamps import (
    BEIJING_TIMEZONE,
    beijing_now_iso,
    normalize_iso_timestamp_to_beijing,
)

logger = logging.getLogger(__name__)


class MidTermUpdater:
    def __init__(self, midterm_memory, llm, config, observation_sink=None, observability_config=None):
        self.midterm_memory = midterm_memory
        self.llm = llm
        self.config = config
        self.observation_sink = observation_sink or NoOpObservationSink()
        self.observability_config = observability_config

    def _context(
        self,
        trace_id: str,
        filters: Dict[str, Any],
        source_job_id: Optional[str],
    ) -> ObservationContext:
        config = self.observability_config
        return ObservationContext(
            trace_id=trace_id,
            capture_payloads=getattr(config, "capture_payloads", True),
            max_payload_length=getattr(config, "max_payload_length", 20000),
            job_id=source_job_id,
            job_type="migration" if source_job_id else None,
            user_id=filters.get("user_id"),
            run_id=filters.get("run_id"),
        )

    def _observability_enabled(self) -> bool:
        return bool(getattr(self.observability_config, "enabled", False))

    def _emit(
        self,
        context: Optional[ObservationContext],
        event_type: str,
        status: str,
        *,
        output_data: Any = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
    ) -> None:
        if context is None:
            return

        def build_event():
            event = context.event(
                stage="midterm",
                event_type=event_type,
                status=status,
                output_data=output_data,
            )
            event.entity_type = entity_type
            event.entity_id = entity_id
            return event

        emit_safely(self.observation_sink, build_event)

    @staticmethod
    def _scope_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value for key, value in (filters or {}).items() if key in ("user_id", "agent_id", "run_id") and value
        }

    @staticmethod
    def _parse_json_response(response: Any) -> Dict[str, Any]:
        if isinstance(response, dict):
            return response
        if not isinstance(response, str):
            return {}
        try:
            cleaned = remove_code_blocks(response)
            return json.loads(cleaned, strict=False)
        except Exception:
            try:
                return json.loads(extract_json(response), strict=False)
            except Exception:
                return {}

    @staticmethod
    def _fallback_keywords(text: str, limit: int = 8) -> List[str]:
        tokens = re.findall(r"\w+%?", text.lower())
        stopwords = {
            "the",
            "and",
            "for",
            "with",
            "that",
            "this",
            "you",
            "your",
            "assistant",
            "user",
        }
        keywords = []
        for token in tokens:
            if len(token) < 2 or token in stopwords or token in keywords:
                continue
            keywords.append(token)
            if len(keywords) >= limit:
                break
        return keywords

    def _summarize_page(
        self,
        user_input: str,
        assistant_response: str,
        *,
        allow_fallback: bool = True,
        observation_context: Optional[ObservationContext] = None,
    ) -> tuple[str, List[str]]:
        raw_dialogue = f"User: {user_input}\nAssistant: {assistant_response}".strip()
        try:
            with observation_stage(
                self.observation_sink,
                observation_context,
                "midterm.page_summary",
                started_event="midterm.page_summary_started",
                succeeded_event="midterm.page_summary_finished",
                failed_event="midterm.page_summary_failed",
                input_data={"input_characters": len(raw_dialogue)},
            ) as state:
                response = self.llm.generate_response(
                    messages=[
                        {"role": "system", "content": MIDTERM_PAGE_SUMMARY_PROMPT},
                        {"role": "user", "content": raw_dialogue},
                    ],
                    response_format={"type": "json_object"},
                )
                if observation_context:
                    state["output"] = {"response_characters": len(str(response))}
                parsed = self._parse_json_response(response)
                summary = str(parsed.get("summary") or "").strip()
                keywords = parsed.get("keywords") or []
                if isinstance(keywords, str):
                    keywords = [item.strip() for item in keywords.split(",") if item.strip()]
                keywords = [str(item).strip() for item in keywords if str(item).strip()]
                if not summary:
                    raise ValueError("midterm page summary response did not contain a summary")
                return summary, keywords[:8] or self._fallback_keywords(raw_dialogue)
        except Exception as exc:
            if not allow_fallback:
                raise
            logger.debug("Midterm page summarization failed; using fallback: %s", exc)
            self._emit(
                observation_context,
                "midterm.page_summary_fallback",
                "succeeded_degraded",
                output_data={"reason": type(exc).__name__},
            )

        summary = user_input.strip() or raw_dialogue[:240]
        if len(summary) > 240:
            summary = f"{summary[:237]}..."
        return summary, self._fallback_keywords(raw_dialogue)

    @staticmethod
    def _messages_to_qa_pairs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        pairs = []
        current: Optional[Dict[str, Any]] = None

        for message in messages or []:
            role = message.get("role")
            content = message.get("content") or ""
            if not content or role == "system":
                continue

            if role == "user":
                if current and current.get("user_input"):
                    pairs.append(current)
                current = {
                    "user_input": content,
                    "assistant_response": "",
                    "created_at": message.get("created_at"),
                }
            elif role == "assistant":
                if current is None:
                    current = {
                        "user_input": "",
                        "assistant_response": content,
                        "created_at": message.get("created_at"),
                    }
                elif current.get("assistant_response"):
                    pairs.append(current)
                    current = {
                        "user_input": "",
                        "assistant_response": content,
                        "created_at": message.get("created_at"),
                    }
                else:
                    current["assistant_response"] = content

        if current and (current.get("user_input") or current.get("assistant_response")):
            pairs.append(current)

        return [pair for pair in pairs if pair.get("user_input")]

    def _latest_page_id(self, filters: Dict[str, Any]) -> Optional[str]:
        rows = self.midterm_memory.list_pages(filters=filters, top_k=10000)
        if not rows:
            return None

        def created_at(row):
            payload = getattr(row, "payload", None) or {}
            value = payload.get("created_at") or ""
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
                return parsed
            except (TypeError, ValueError):
                return datetime.min.replace(tzinfo=BEIJING_TIMEZONE)

        rows.sort(key=created_at)
        return str(rows[-1].id)

    def _session_id_for_page(self, page_id: str, filters: Dict[str, Any]) -> Optional[str]:
        for session in self.midterm_memory.list_sessions(filters=filters, top_k=10000):
            payload = getattr(session, "payload", None) or {}
            if page_id in (payload.get("page_ids") or []):
                return str(session.id)
        return None

    def _link_previous_page(self, previous_page_id: Optional[str], page_id: str) -> None:
        if not previous_page_id:
            return
        previous = self.midterm_memory.get_page(previous_page_id)
        if not previous:
            return
        payload = dict(getattr(previous, "payload", None) or {})
        payload["next_page"] = page_id
        payload["updated_at"] = beijing_now_iso()
        try:
            self.midterm_memory.update_page(previous_page_id, payload, reembed=False)
        except Exception as exc:
            logger.debug("Failed to update midterm previous page link: %s", exc)

    @staticmethod
    def _dedupe_keywords(*keyword_lists: List[str], limit: int = 12) -> List[str]:
        keywords = []
        seen = set()
        for keyword_list in keyword_lists:
            for keyword in keyword_list or []:
                normalized = str(keyword).strip()
                key = normalized.lower()
                if not normalized or key in seen:
                    continue
                seen.add(key)
                keywords.append(normalized)
                if len(keywords) >= limit:
                    return keywords
        return keywords

    def _fallback_merge_session(
        self,
        existing_summary: str,
        existing_keywords: List[str],
        page_summary: str,
        page_keywords: List[str],
    ) -> tuple[str, List[str]]:
        existing_summary = (existing_summary or "").strip()
        page_summary = (page_summary or "").strip()
        if not existing_summary:
            summary = page_summary
        elif page_summary and page_summary.lower() not in existing_summary.lower():
            summary = f"{existing_summary} {page_summary}"
        else:
            summary = existing_summary
        return summary[:1000], self._dedupe_keywords(existing_keywords, page_keywords)

    def _merge_session(
        self,
        existing_summary: str,
        existing_keywords: List[str],
        page_summary: str,
        page_keywords: List[str],
        *,
        allow_fallback: bool = True,
        observation_context: Optional[ObservationContext] = None,
    ) -> tuple[str, List[str]]:
        fallback = self._fallback_merge_session(
            existing_summary,
            existing_keywords,
            page_summary,
            page_keywords,
        )

        merge_input = {
            "existing_session": {
                "summary": existing_summary or "",
                "keywords": existing_keywords or [],
            },
            "new_page": {
                "summary": page_summary or "",
                "keywords": page_keywords or [],
            },
        }

        try:
            response = self.llm.generate_response(
                messages=[
                    {"role": "system", "content": MIDTERM_SESSION_MERGE_PROMPT},
                    {"role": "user", "content": json.dumps(merge_input, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
            )
            parsed = self._parse_json_response(response)
            summary = str(parsed.get("summary") or "").strip()
            keywords = parsed.get("keywords") or []
            if isinstance(keywords, str):
                keywords = [item.strip() for item in keywords.split(",") if item.strip()]
            keywords = self._dedupe_keywords(keywords, existing_keywords, page_keywords)
            if summary:
                return summary[:1000], keywords[:12] or fallback[1]
            raise ValueError("midterm session merge response did not contain a summary")
        except Exception as exc:
            self._emit(
                observation_context,
                "midterm.session_merge_failed",
                "failed",
                output_data={"reason": type(exc).__name__},
            )
            if not allow_fallback:
                raise
            logger.debug("Midterm session merge failed; using fallback: %s", exc)
            self._emit(
                observation_context,
                "midterm.session_merge_fallback",
                "succeeded_degraded",
                output_data={"reason": type(exc).__name__},
            )

        return fallback

    def _append_page_to_session(
        self,
        session_id: str,
        page_payload: Dict[str, Any],
        *,
        allow_fallback: bool = True,
        use_llm: bool = True,
    ) -> str:
        session = self.midterm_memory.get_session(session_id)
        if not session:
            return self._create_session(page_payload)

        payload = dict(getattr(session, "payload", None) or {})
        page_ids = list(payload.get("page_ids") or [])
        if page_payload["id"] in page_ids:
            return session_id
        page_ids.append(page_payload["id"])
        observation_context = page_payload.get("_observation_context")

        if use_llm:
            summary, keywords = self._merge_session(
                payload.get("summary", ""),
                payload.get("summary_keywords") or [],
                page_payload.get("summary", ""),
                page_payload.get("keywords") or [],
                allow_fallback=allow_fallback,
                observation_context=observation_context,
            )
        else:
            summary, keywords = self._fallback_merge_session(
                payload.get("summary", ""),
                payload.get("summary_keywords") or [],
                page_payload.get("summary", ""),
                page_payload.get("keywords") or [],
            )
        payload.update(
            {
                "summary": summary,
                "summary_keywords": keywords,
                "page_ids": page_ids,
                "L_interaction": len(page_ids),
                "updated_at": beijing_now_iso(),
            }
        )
        if page_payload.get("trace_id"):
            payload.setdefault("created_trace_id", payload.get("trace_id") or page_payload["trace_id"])
            payload.setdefault(
                "created_source_job_id",
                payload.get("source_job_id") or page_payload.get("source_job_id"),
            )
            payload["last_updated_trace_id"] = page_payload["trace_id"]
            payload["last_updated_source_job_id"] = page_payload.get("source_job_id")
        payload["R_recency"] = float(payload.get("R_recency", 1.0) or 1.0)
        payload["H_segment"] = compute_session_heat(payload, self.config)
        self.midterm_memory.update_session(session_id, payload, reembed=True)
        if observation_context:
            self._emit(
                observation_context,
                "midterm.session_merged",
                "succeeded",
                output_data={"page_count": len(page_ids)},
                entity_type="midterm_session",
                entity_id=session_id,
            )
        return session_id

    def _create_session(self, page_payload: Dict[str, Any], session_id: Optional[str] = None) -> str:
        now = beijing_now_iso()
        session_id = session_id or str(uuid.uuid4())
        payload = {
            "id": session_id,
            "summary": page_payload.get("summary", ""),
            "summary_keywords": list(page_payload.get("keywords") or []),
            "page_ids": [page_payload["id"]],
            "N_visit": 0,
            "L_interaction": 1,
            "R_recency": 1.0,
            "H_segment": 0.0,
            "created_at": now,
            "updated_at": now,
            "last_visit_time": now,
            "user_id": page_payload.get("user_id"),
            "agent_id": page_payload.get("agent_id"),
            "run_id": page_payload.get("run_id"),
            "source_job_id": page_payload.get("source_job_id"),
        }
        if page_payload.get("trace_id"):
            payload.update(
                {
                    "created_trace_id": page_payload["trace_id"],
                    "created_source_job_id": page_payload.get("source_job_id"),
                    "last_updated_trace_id": page_payload["trace_id"],
                    "last_updated_source_job_id": page_payload.get("source_job_id"),
                }
            )
        payload["H_segment"] = compute_session_heat(payload, self.config)
        self.midterm_memory.insert_session(session_id, payload)
        observation_context = page_payload.get("_observation_context")
        if observation_context:
            self._emit(
                observation_context,
                "midterm.session_created",
                "succeeded",
                entity_type="midterm_session",
                entity_id=session_id,
            )
        return session_id

    def _assign_session(
        self,
        page_payload: Dict[str, Any],
        filters: Dict[str, Any],
        *,
        allow_fallback: bool = True,
        use_llm: bool = True,
        new_session_id: Optional[str] = None,
    ) -> str:
        query = self.midterm_memory.page_embedding_text(page_payload)
        candidate_sessions = self.midterm_memory.search_sessions(
            query=query,
            filters=filters,
            top_k=self.config.top_k_sessions,
        )
        observation_context = page_payload.get("_observation_context")
        if observation_context:
            self._emit(
                observation_context,
                "midterm.session_candidates_found",
                "succeeded",
                output_data={"candidate_count": len(candidate_sessions)},
            )

        best_session_id = None
        best_score = -1.0
        candidate_scores = []
        for session in candidate_sessions:
            payload = getattr(session, "payload", None) or {}
            embedding_score = float(getattr(session, "score", 0.0) or 0.0)
            overlap_score = keyword_overlap(page_payload.get("keywords") or [], payload.get("summary_keywords") or [])
            combined_score = (
                self.config.embedding_similarity_weight * embedding_score
                + self.config.keyword_overlap_weight * overlap_score
            )
            if observation_context:
                score = {
                    "session_id": str(session.id),
                    "embedding_similarity": embedding_score,
                    "keyword_score": overlap_score,
                    "combined_score": combined_score,
                    "threshold": self.config.session_similarity_threshold,
                }
                candidate_scores.append(score)
                self._emit(
                    observation_context,
                    "midterm.session_score_computed",
                    "succeeded",
                    output_data=score,
                    entity_type="midterm_session",
                    entity_id=str(session.id),
                )
            if combined_score > best_score:
                best_score = combined_score
                best_session_id = str(session.id)

        matched = bool(best_session_id and best_score >= self.config.session_similarity_threshold)
        if observation_context:
            self._emit(
                observation_context,
                "midterm.session_score_computed",
                "succeeded",
                output_data={
                    "candidates": candidate_scores,
                    "threshold": self.config.session_similarity_threshold,
                    "decision": "merge" if matched else "create",
                    "selected_session_id": best_session_id if matched else new_session_id,
                },
            )
        if matched:
            return self._append_page_to_session(
                best_session_id,
                page_payload,
                allow_fallback=allow_fallback,
                use_llm=use_llm,
            )
        return self._create_session(page_payload, session_id=new_session_id)

    def process_evicted_messages(
        self,
        evicted_messages: List[Dict[str, Any]],
        filters: Dict[str, Any],
        *,
        source_job_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        degraded: bool = False,
    ) -> List[Dict[str, Any]]:
        effective_trace_id = trace_id if self._observability_enabled() else None
        context = (
            self._context(effective_trace_id, self._scope_filters(filters), source_job_id)
            if effective_trace_id
            else None
        )
        event_type = "midterm.degraded" if degraded else "midterm.succeeded"
        with observation_stage(
            self.observation_sink,
            context,
            "midterm",
            started_event="midterm.started",
            succeeded_event=event_type,
            failed_event="midterm.failed",
            input_data={"message_count": len(evicted_messages or [])},
        ) as state:
            pages = self._process_evicted_messages(
                evicted_messages,
                filters,
                source_job_id=source_job_id,
                trace_id=effective_trace_id,
                degraded=degraded,
                observation_context=context,
            )
            if context:
                state["output"] = {"page_count": len(pages)}
            return pages

    def _process_evicted_messages(
        self,
        evicted_messages: List[Dict[str, Any]],
        filters: Dict[str, Any],
        *,
        source_job_id: Optional[str],
        trace_id: Optional[str],
        degraded: bool,
        observation_context: Optional[ObservationContext],
    ) -> List[Dict[str, Any]]:
        scope_filters = self._scope_filters(filters)
        if not evicted_messages or not scope_filters:
            return []

        pages = []
        previous_page_id = self._latest_page_id(scope_filters)
        qa_pairs = self._messages_to_qa_pairs(evicted_messages)
        for index, qa_pair in enumerate(qa_pairs):
            self._emit(
                observation_context,
                "midterm.qa_pair_created",
                "succeeded",
                output_data={"index": index, "qa_pair_count": len(qa_pairs)},
            )
            page_id = (
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:midterm:{source_job_id}:{index}"))
                if source_job_id
                else str(uuid.uuid4())
            )
            now = beijing_now_iso()
            raw_dialogue = (
                f"User: {qa_pair.get('user_input', '')}\nAssistant: {qa_pair.get('assistant_response', '')}"
            ).strip()
            existing_page = self.midterm_memory.get_page(page_id) if source_job_id else None
            if existing_page:
                page_payload = dict(getattr(existing_page, "payload", None) or {})
                page_payload.setdefault("id", page_id)
                if page_payload.get("session_id"):
                    pages.append(page_payload)
                    previous_page_id = page_id
                    continue
                if degraded:
                    page_payload["degraded"] = True
                    page_payload["needs_reprocessing"] = True
                    page_payload["updated_at"] = now
                    self.midterm_memory.update_page(page_id, page_payload, reembed=False)
            else:
                if degraded:
                    summary = qa_pair.get("user_input", "").strip() or raw_dialogue[:240]
                    keywords = self._fallback_keywords(raw_dialogue)
                else:
                    summary, keywords = self._summarize_page(
                        qa_pair.get("user_input", ""),
                        qa_pair.get("assistant_response", ""),
                        allow_fallback=source_job_id is None,
                        observation_context=observation_context,
                    )
                created_at = normalize_iso_timestamp_to_beijing(qa_pair.get("created_at")) or now
                page_payload = {
                    "id": page_id,
                    "session_id": None,
                    "raw_dialogue": raw_dialogue,
                    "user_input": qa_pair.get("user_input", ""),
                    "assistant_response": qa_pair.get("assistant_response", ""),
                    "summary": summary,
                    "keywords": keywords,
                    "pre_page": previous_page_id,
                    "next_page": None,
                    "created_at": created_at,
                    "updated_at": now,
                    "user_id": scope_filters.get("user_id"),
                    "agent_id": scope_filters.get("agent_id"),
                    "run_id": scope_filters.get("run_id"),
                    "source_job_id": source_job_id,
                    "degraded": degraded,
                    "needs_reprocessing": degraded,
                }
                if trace_id:
                    page_payload["trace_id"] = trace_id
                if observation_context:
                    page_payload["_observation_context"] = observation_context
                stored_page_payload = {
                    key: value for key, value in page_payload.items() if key != "_observation_context"
                }
                self.midterm_memory.insert_page(page_id, stored_page_payload)
                self._emit(
                    observation_context,
                    "midterm.page_written",
                    "succeeded",
                    entity_type="midterm_page",
                    entity_id=page_id,
                )
            if observation_context:
                page_payload["_observation_context"] = observation_context
            self._link_previous_page(previous_page_id, page_id)

            new_session_id = (
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:midterm-session:{source_job_id}:{index}"))
                if source_job_id
                else None
            )
            session_id = self._session_id_for_page(page_id, scope_filters)
            if session_id is None:
                session_id = self._assign_session(
                    page_payload,
                    scope_filters,
                    allow_fallback=source_job_id is None,
                    use_llm=not degraded,
                    new_session_id=new_session_id,
                )
            page_payload["session_id"] = session_id
            page_payload["updated_at"] = beijing_now_iso()
            stored_page_payload = {key: value for key, value in page_payload.items() if key != "_observation_context"}
            self.midterm_memory.update_page(page_id, stored_page_payload, reembed=False)
            self._emit(
                observation_context,
                "midterm.page_linked",
                "succeeded",
                output_data={"session_id": session_id},
                entity_type="midterm_page",
                entity_id=page_id,
            )
            pages.append(stored_page_payload)
            previous_page_id = page_id

        return pages

    def promote_hot_sessions(self) -> List[Dict[str, Any]]:
        hot_sessions = []
        for session in self.midterm_memory.list_sessions(top_k=10000):
            payload = getattr(session, "payload", None) or {}
            if float(payload.get("H_segment", 0.0) or 0.0) >= self.config.promotion_heat_threshold:
                hot_sessions.append({"id": str(session.id), **payload})
        return hot_sessions

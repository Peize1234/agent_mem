import asyncio
import inspect
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT
from mem0.memory.midterm import compute_recency, compute_session_heat, keyword_overlap
from mem0.memory.utils import extract_json, remove_code_blocks
from mem0.utils.timestamps import (
    BEIJING_TIMEZONE,
    beijing_now_iso,
    normalize_iso_timestamp_to_beijing,
)

logger = logging.getLogger(__name__)

PRODUCTION_PAGE_CONTEXT_CONTRACT = "production_previous_current_following_raw_v1"


def _format_page_dialogue(user_input: Any, assistant_response: Any) -> str:
    """Format one page identically for the summary model and persisted raw dialogue."""
    user_text = "" if user_input is None else str(user_input)
    assistant_text = "" if assistant_response is None else str(assistant_response)
    user_line = f"User:{' ' if user_text else ''}{user_text}"
    assistant_line = f"Assistant:{' ' if assistant_text else ''}{assistant_text}"
    return f"{user_line}\n\n{assistant_line}"


def build_midterm_page_summary_input(
    previous_raw_dialogues: List[str],
    user_input: Any,
    assistant_response: Any,
    following_raw_dialogues: List[str],
) -> str:
    """Build the fixed Production previous/current/following Page context contract."""
    previous = [str(value).strip() for value in previous_raw_dialogues if str(value).strip()]
    following = [str(value).strip() for value in following_raw_dialogues if str(value).strip()]
    sections = ["上文：", "\n\n".join(previous) if previous else "（无）"]
    sections.extend(
        [
            "当前待总结对话：",
            _format_page_dialogue(user_input, assistant_response),
            "下文：",
            "\n\n".join(following) if following else "（无）",
        ]
    )
    return "\n\n".join(sections)


class MidTermUpdater:
    def __init__(self, midterm_memory, llm, config, *, following_qa_provider=None):
        self.midterm_memory = midterm_memory
        self.llm = llm
        self.config = config
        self.following_qa_provider = following_qa_provider

    @staticmethod
    def _scope_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value for key, value in (filters or {}).items() if key in ("user_id", "agent_id", "run_id") and value
        }

    def _current_turn_index(self, payload: Dict[str, Any]) -> int:
        scope_filters = self._scope_filters(payload)
        return int(self.midterm_memory.current_turn_index(scope_filters))

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
        previous_raw_dialogues: Optional[List[str]] = None,
        following_raw_dialogues: Optional[List[str]] = None,
        allow_fallback: bool = True,
    ) -> tuple[str, List[str]]:
        raw_dialogue = _format_page_dialogue(user_input, assistant_response)
        summary_input = build_midterm_page_summary_input(
            previous_raw_dialogues or [],
            user_input,
            assistant_response,
            following_raw_dialogues or [],
        )
        try:
            response = self.llm.generate_response(
                messages=[
                    {"role": "system", "content": MIDTERM_PAGE_SUMMARY_PROMPT},
                    {"role": "user", "content": summary_input},
                ],
                response_format={"type": "json_object"},
            )
            parsed = self._parse_json_response(response)
            summary = str(parsed.get("summary") or "").strip()
            keywords = parsed.get("keywords") or []
            if isinstance(keywords, str):
                keywords = [item.strip() for item in keywords.split(",") if item.strip()]
            keywords = [str(item).strip() for item in keywords if str(item).strip()]
            if summary:
                return summary, keywords[:8] or self._fallback_keywords(raw_dialogue)
            if not allow_fallback:
                raise ValueError("midterm page summary response did not contain a summary")
        except Exception as exc:
            if not allow_fallback:
                raise
            logger.debug("Midterm page summarization failed; using fallback: %s", exc)

        summary = user_input.strip() or raw_dialogue[:240]
        if len(summary) > 240:
            summary = f"{summary[:237]}..."
        return summary, self._fallback_keywords(raw_dialogue)

    async def _generate_response_async(self, **kwargs):
        async_generate = getattr(self.llm, "generate_response_async", None)
        if inspect.iscoroutinefunction(async_generate):
            return await async_generate(**kwargs)
        return await asyncio.to_thread(self.llm.generate_response, **kwargs)

    async def _summarize_page_async(
        self,
        user_input: str,
        assistant_response: str,
        *,
        previous_raw_dialogues: Optional[List[str]] = None,
        following_raw_dialogues: Optional[List[str]] = None,
        allow_fallback: bool = True,
    ) -> tuple[str, List[str]]:
        raw_dialogue = _format_page_dialogue(user_input, assistant_response)
        summary_input = build_midterm_page_summary_input(
            previous_raw_dialogues or [],
            user_input,
            assistant_response,
            following_raw_dialogues or [],
        )
        try:
            response = await self._generate_response_async(
                messages=[
                    {"role": "system", "content": MIDTERM_PAGE_SUMMARY_PROMPT},
                    {"role": "user", "content": summary_input},
                ],
                response_format={"type": "json_object"},
            )
            parsed = self._parse_json_response(response)
            summary = str(parsed.get("summary") or "").strip()
            keywords = parsed.get("keywords") or []
            if isinstance(keywords, str):
                keywords = [item.strip() for item in keywords.split(",") if item.strip()]
            keywords = [str(item).strip() for item in keywords if str(item).strip()]
            if summary:
                return summary, keywords[:8] or self._fallback_keywords(raw_dialogue)
            if not allow_fallback:
                raise ValueError("midterm page summary response did not contain a summary")
        except Exception as exc:
            if not allow_fallback:
                raise
            logger.debug("Midterm page summarization failed; using fallback: %s", exc)

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
                    "turn_index": message.get("turn_index"),
                }
            elif role == "assistant":
                if current is None:
                    current = {
                        "user_input": "",
                        "assistant_response": content,
                        "created_at": message.get("created_at"),
                        "turn_index": message.get("turn_index"),
                    }
                elif current.get("assistant_response"):
                    pairs.append(current)
                    current = {
                        "user_input": "",
                        "assistant_response": content,
                        "created_at": message.get("created_at"),
                        "turn_index": message.get("turn_index"),
                    }
                else:
                    current["assistant_response"] = content

        if current and (current.get("user_input") or current.get("assistant_response")):
            pairs.append(current)

        return [pair for pair in pairs if pair.get("user_input")]

    @staticmethod
    def _turn_indices(qa_pairs: List[Dict[str, Any]]) -> List[int]:
        turn_indices = []
        for pair in qa_pairs:
            turn_index = pair.get("turn_index")
            if turn_index is None or int(turn_index) <= 0:
                raise ValueError("Mid-term source QA is missing a positive turn_index")
            turn_indices.append(int(turn_index))
        return turn_indices

    def _latest_page_id(self, filters: Dict[str, Any]) -> Optional[str]:
        rows = self.midterm_memory.list_pages(filters=filters, top_k=10000)
        if not rows:
            return None

        def page_order(row):
            payload = getattr(row, "payload", None) or {}
            sequence = payload.get("page_sequence")
            if sequence is not None:
                return (1, int(sequence), datetime.min.replace(tzinfo=BEIJING_TIMEZONE))
            value = payload.get("created_at") or ""
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
                return (0, 0, parsed)
            except (TypeError, ValueError):
                return (0, 0, datetime.min.replace(tzinfo=BEIJING_TIMEZONE))

        rows.sort(key=page_order)
        return str(rows[-1].id)

    def _page_context_qa_window(self) -> int:
        return max(int(getattr(self.config, "short_term_capacity", 0)) // 2, 0)

    def _previous_raw_dialogues(self, previous_page_id: Optional[str]) -> List[str]:
        """Walk the persisted Page chain instead of using semantic adjacency."""
        remaining = self._page_context_qa_window()
        page_id = previous_page_id
        newest_first = []
        seen = set()
        while page_id and remaining > 0 and page_id not in seen:
            seen.add(page_id)
            page = self.midterm_memory.get_page(page_id)
            if page is None:
                break
            payload = dict(getattr(page, "payload", None) or {})
            raw_dialogue = str(payload.get("raw_dialogue") or "").strip()
            if raw_dialogue:
                newest_first.append(raw_dialogue)
                remaining -= 1
            page_id = payload.get("pre_page")
        newest_first.reverse()
        return newest_first

    def _following_raw_dialogues(
        self,
        scope_filters: Dict[str, Any],
        source_turn_index: int,
    ) -> List[str]:
        if self.following_qa_provider is None or self._page_context_qa_window() <= 0:
            return []
        messages = self.following_qa_provider(
            scope_filters,
            source_turn_index,
            self._page_context_qa_window(),
        )
        return [
            _format_page_dialogue(pair.get("user_input", ""), pair.get("assistant_response", ""))
            for pair in self._messages_to_qa_pairs(messages)
            if pair.get("user_input") and pair.get("assistant_response")
        ]

    def _session_id_for_page(
        self,
        page_id: str,
        filters: Dict[str, Any],
        *,
        include_uncommitted: bool = False,
    ) -> Optional[str]:
        for session in self.midterm_memory.list_sessions(
            filters=filters,
            top_k=10000,
            include_uncommitted=include_uncommitted,
        ):
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

    def _take_over_staging_session_output(
        self,
        page_payload: Dict[str, Any],
        source_job_id: str,
        lease_token: str,
        lease_is_current,
    ) -> None:
        session_id = page_payload.get("_commit_session_id") or page_payload.get("session_id")
        if not session_id:
            return
        session = self.midterm_memory.get_session(session_id)
        if session is None:
            return
        session_payload = dict(getattr(session, "payload", None) or {})
        changed = False
        if (
            session_payload.get("output_state") == "staging"
            and session_payload.get("created_by_source_job_id") == source_job_id
        ):
            session_payload["output_lease_token"] = lease_token
            session_payload["created_by_lease_token"] = lease_token
            changed = True
        elif (
            page_payload.get("_commit_session_backup") is not None
            and session_payload.get("last_output_job_id") == source_job_id
        ):
            session_payload["last_output_lease_token"] = lease_token
            changed = True
        if changed:
            if lease_is_current is not None and not lease_is_current():
                raise RuntimeError("stale migration stage lease")
            self.midterm_memory.update_session(session_id, session_payload, reembed=False)

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
                    {
                        "role": "user",
                        "content": json.dumps(merge_input, ensure_ascii=False, indent=2),
                    },
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
            if not allow_fallback:
                raise ValueError("midterm session merge response did not contain a summary")
        except Exception as exc:
            if not allow_fallback:
                raise
            logger.debug("Midterm session merge failed; using fallback: %s", exc)

        return fallback

    async def _merge_session_async(
        self,
        existing_summary: str,
        existing_keywords: List[str],
        page_summary: str,
        page_keywords: List[str],
        *,
        allow_fallback: bool = True,
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
            response = await self._generate_response_async(
                messages=[
                    {"role": "system", "content": MIDTERM_SESSION_MERGE_PROMPT},
                    {"role": "user", "content": json.dumps(merge_input, ensure_ascii=False, indent=2)},
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
            if not allow_fallback:
                raise ValueError("midterm session merge response did not contain a summary")
        except Exception as exc:
            if not allow_fallback:
                raise
            logger.debug("Midterm session merge failed; using fallback: %s", exc)
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
        source_job_ids = list(payload.get("source_job_ids") or [])
        initial_source_job_id = payload.get("source_job_id")
        if initial_source_job_id and initial_source_job_id not in source_job_ids:
            source_job_ids.append(initial_source_job_id)
        page_source_job_id = page_payload.get("source_job_id")
        if page_source_job_id and page_source_job_id not in source_job_ids:
            source_job_ids.append(page_source_job_id)

        if use_llm:
            summary, keywords = self._merge_session(
                payload.get("summary", ""),
                payload.get("summary_keywords") or [],
                page_payload.get("summary", ""),
                page_payload.get("keywords") or [],
                allow_fallback=allow_fallback,
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
                "source_job_ids": source_job_ids,
                "L_interaction": len(page_ids),
                "updated_at": beijing_now_iso(),
            }
        )
        current_turn_index = self._current_turn_index(page_payload)
        payload["R_recency"] = compute_recency(
            int(payload["last_visit_turn_index"]),
            current_turn_index,
            self.config.heat_recency_tau_turns,
        )
        payload["H_segment"] = compute_session_heat(payload, self.config, current_turn_index)
        if page_source_job_id and page_payload.get("output_state") == "staging":
            page_payload["_commit_session_id"] = session_id
            page_payload["_commit_session_backup"] = dict(getattr(session, "payload", None) or {})
            self.midterm_memory.update_page(page_payload["id"], page_payload, reembed=False)
            payload["last_output_job_id"] = page_source_job_id
            payload["last_output_lease_token"] = page_payload.get("output_lease_token")
        self.midterm_memory.update_session(session_id, payload, reembed=True)
        return session_id

    async def _append_page_to_session_async(
        self,
        session_id: str,
        page_payload: Dict[str, Any],
        *,
        allow_fallback: bool = True,
        use_llm: bool = True,
    ) -> str:
        session = await asyncio.to_thread(self.midterm_memory.get_session, session_id)
        if not session:
            return await asyncio.to_thread(self._create_session, page_payload)

        payload = dict(getattr(session, "payload", None) or {})
        page_ids = list(payload.get("page_ids") or [])
        if page_payload["id"] in page_ids:
            return session_id
        page_ids.append(page_payload["id"])
        source_job_ids = list(payload.get("source_job_ids") or [])
        initial_source_job_id = payload.get("source_job_id")
        if initial_source_job_id and initial_source_job_id not in source_job_ids:
            source_job_ids.append(initial_source_job_id)
        page_source_job_id = page_payload.get("source_job_id")
        if page_source_job_id and page_source_job_id not in source_job_ids:
            source_job_ids.append(page_source_job_id)

        if use_llm:
            summary, keywords = await self._merge_session_async(
                payload.get("summary", ""),
                payload.get("summary_keywords") or [],
                page_payload.get("summary", ""),
                page_payload.get("keywords") or [],
                allow_fallback=allow_fallback,
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
                "source_job_ids": source_job_ids,
                "L_interaction": len(page_ids),
                "updated_at": beijing_now_iso(),
            }
        )
        current_turn_index = self._current_turn_index(page_payload)
        payload["R_recency"] = compute_recency(
            int(payload["last_visit_turn_index"]),
            current_turn_index,
            self.config.heat_recency_tau_turns,
        )
        payload["H_segment"] = compute_session_heat(payload, self.config, current_turn_index)
        if page_source_job_id and page_payload.get("output_state") == "staging":
            page_payload["_commit_session_id"] = session_id
            page_payload["_commit_session_backup"] = dict(getattr(session, "payload", None) or {})
            await asyncio.to_thread(
                self.midterm_memory.update_page,
                page_payload["id"],
                page_payload,
                reembed=False,
            )
            payload["last_output_job_id"] = page_source_job_id
            payload["last_output_lease_token"] = page_payload.get("output_lease_token")
        await asyncio.to_thread(self.midterm_memory.update_session, session_id, payload, reembed=True)
        return session_id

    def _create_session(self, page_payload: Dict[str, Any], session_id: Optional[str] = None) -> str:
        now = beijing_now_iso()
        session_id = session_id or str(uuid.uuid4())
        created_turn_index = page_payload["turn_index"]
        payload = {
            "id": session_id,
            "summary": page_payload.get("summary", ""),
            "summary_keywords": list(page_payload.get("keywords") or []),
            "page_ids": [page_payload["id"]],
            "N_visit": 0,
            "valid_recall_count": 0,
            "last_recall_at": None,
            "L_interaction": 1,
            "R_recency": 1.0,
            "H_segment": 0.0,
            "created_turn_index": created_turn_index,
            "last_visit_turn_index": created_turn_index,
            "created_at": now,
            "updated_at": now,
            "user_id": page_payload.get("user_id"),
            "agent_id": page_payload.get("agent_id"),
            "run_id": page_payload.get("run_id"),
            "source_job_id": page_payload.get("source_job_id"),
            "source_job_ids": [page_payload["source_job_id"]] if page_payload.get("source_job_id") else [],
            "source_stage": page_payload.get("source_stage"),
            "output_state": page_payload.get("output_state", "committed"),
            "output_lease_token": page_payload.get("output_lease_token"),
            "created_by_source_job_id": page_payload.get("source_job_id"),
            "created_by_lease_token": page_payload.get("output_lease_token"),
        }
        current_turn_index = self._current_turn_index(page_payload)
        payload["R_recency"] = compute_recency(
            created_turn_index,
            current_turn_index,
            self.config.heat_recency_tau_turns,
        )
        payload["H_segment"] = compute_session_heat(payload, self.config, current_turn_index)
        self.midterm_memory.insert_session(session_id, payload)
        return session_id

    def _assign_session(
        self,
        page_payload: Dict[str, Any],
        filters: Dict[str, Any],
        *,
        allow_fallback: bool = True,
        use_llm: bool = True,
        new_session_id: Optional[str] = None,
        include_uncommitted: bool = False,
    ) -> str:
        query = self.midterm_memory.page_embedding_text(page_payload)
        search_kwargs = {
            "query": query,
            "filters": filters,
            "top_k": self.config.top_k_sessions,
        }
        if include_uncommitted:
            search_kwargs["include_uncommitted"] = True
        candidate_sessions = self.midterm_memory.search_sessions(**search_kwargs)

        best_session_id = None
        best_score = -1.0
        for session in candidate_sessions:
            payload = getattr(session, "payload", None) or {}
            if include_uncommitted and not self.midterm_memory.output_is_visible(payload):
                owner_job_id = payload.get("last_output_job_id") or payload.get("source_job_id")
                if owner_job_id != page_payload.get("source_job_id"):
                    continue
            embedding_score = float(getattr(session, "score", 0.0) or 0.0)
            overlap_score = keyword_overlap(page_payload.get("keywords") or [], payload.get("summary_keywords") or [])
            combined_score = (
                self.config.embedding_similarity_weight * embedding_score
                + self.config.keyword_overlap_weight * overlap_score
            )
            if combined_score > best_score:
                best_score = combined_score
                best_session_id = str(session.id)

        if best_session_id and best_score >= self.config.session_similarity_threshold:
            return self._append_page_to_session(
                best_session_id,
                page_payload,
                allow_fallback=allow_fallback,
                use_llm=use_llm,
            )
        return self._create_session(page_payload, session_id=new_session_id)

    async def _assign_session_async(
        self,
        page_payload: Dict[str, Any],
        filters: Dict[str, Any],
        *,
        allow_fallback: bool = True,
        use_llm: bool = True,
        new_session_id: Optional[str] = None,
        include_uncommitted: bool = False,
    ) -> str:
        query = self.midterm_memory.page_embedding_text(page_payload)
        search_kwargs = {
            "query": query,
            "filters": filters,
            "top_k": self.config.top_k_sessions,
        }
        if include_uncommitted:
            search_kwargs["include_uncommitted"] = True
        candidate_sessions = await asyncio.to_thread(self.midterm_memory.search_sessions, **search_kwargs)

        best_session_id = None
        best_score = -1.0
        for session in candidate_sessions:
            payload = getattr(session, "payload", None) or {}
            if include_uncommitted and not self.midterm_memory.output_is_visible(payload):
                owner_job_id = payload.get("last_output_job_id") or payload.get("source_job_id")
                if owner_job_id != page_payload.get("source_job_id"):
                    continue
            embedding_score = float(getattr(session, "score", 0.0) or 0.0)
            overlap_score = keyword_overlap(page_payload.get("keywords") or [], payload.get("summary_keywords") or [])
            combined_score = (
                self.config.embedding_similarity_weight * embedding_score
                + self.config.keyword_overlap_weight * overlap_score
            )
            if combined_score > best_score:
                best_score = combined_score
                best_session_id = str(session.id)

        if best_session_id and best_score >= self.config.session_similarity_threshold:
            return await self._append_page_to_session_async(
                best_session_id,
                page_payload,
                allow_fallback=allow_fallback,
                use_llm=use_llm,
            )
        return await asyncio.to_thread(self._create_session, page_payload, session_id=new_session_id)

    def process_evicted_messages(
        self,
        evicted_messages: List[Dict[str, Any]],
        filters: Dict[str, Any],
        *,
        source_job_id: Optional[str] = None,
        lease_token: Optional[str] = None,
        lease_is_current=None,
        degraded: bool = False,
    ) -> List[Dict[str, Any]]:
        scope_filters = self._scope_filters(filters)
        if not evicted_messages or not scope_filters:
            return []

        qa_pairs = self._messages_to_qa_pairs(evicted_messages)
        turn_indices = self._turn_indices(qa_pairs)
        page_ids = [
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:midterm:{source_job_id}:{index}"))
            if source_job_id
            else str(uuid.uuid4())
            for index in range(len(qa_pairs))
        ]
        existing_pages = (
            {page_id: self.midterm_memory.get_page(page_id) for page_id in page_ids}
            if source_job_id
            else {page_id: None for page_id in page_ids}
        )
        sequences_to_reserve = sum(
            not existing_page or (getattr(existing_page, "payload", None) or {}).get("page_sequence") is None
            for existing_page in existing_pages.values()
        )
        reserved_sequences = iter(self.midterm_memory.reserve_page_sequences(scope_filters, sequences_to_reserve))
        pages = []
        previous_page_id = self._latest_page_id(scope_filters)
        for index, qa_pair in enumerate(qa_pairs):
            if lease_is_current is not None and not lease_is_current():
                raise RuntimeError("stale migration stage lease")
            page_id = page_ids[index]
            turn_index = turn_indices[index]
            now = beijing_now_iso()
            raw_dialogue = _format_page_dialogue(
                qa_pair.get("user_input", ""),
                qa_pair.get("assistant_response", ""),
            )
            existing_page = existing_pages.get(page_id)
            page_sequence = (getattr(existing_page, "payload", None) or {}).get("page_sequence")
            if page_sequence is None:
                page_sequence = next(reserved_sequences)
            if existing_page:
                page_payload = dict(getattr(existing_page, "payload", None) or {})
                page_payload.setdefault("id", page_id)
                page_payload.setdefault("page_sequence", page_sequence)
                if int(page_payload["turn_index"]) != turn_index:
                    raise RuntimeError("existing Mid-term Page turn_index does not match its source messages")
                if source_job_id:
                    page_payload.update(
                        {
                            "source_stage": "midterm",
                            "output_state": "staging",
                            "output_lease_token": lease_token,
                            "degraded": degraded,
                            "needs_reprocessing": degraded,
                            "updated_at": now,
                        }
                    )
                    if lease_is_current is not None and not lease_is_current():
                        raise RuntimeError("stale migration stage lease")
                    self.midterm_memory.update_page(page_id, page_payload, reembed=False)
                    self._take_over_staging_session_output(
                        page_payload,
                        source_job_id,
                        lease_token,
                        lease_is_current,
                    )
                    pages.append(page_payload)
                    previous_page_id = page_id
                    continue
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
                    previous_raw_dialogues = self._previous_raw_dialogues(previous_page_id)
                    following_raw_dialogues = self._following_raw_dialogues(scope_filters, turn_index)
                    summary, keywords = self._summarize_page(
                        qa_pair.get("user_input", ""),
                        qa_pair.get("assistant_response", ""),
                        previous_raw_dialogues=previous_raw_dialogues,
                        following_raw_dialogues=following_raw_dialogues,
                        allow_fallback=source_job_id is None,
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
                    "source_stage": "midterm" if source_job_id else None,
                    "output_state": "staging" if source_job_id else "committed",
                    "output_lease_token": lease_token,
                    "degraded": degraded,
                    "needs_reprocessing": degraded,
                    "valid_recall_count": 0,
                    "last_recall_at": None,
                    "last_recall_turn_index": None,
                    "page_sequence": page_sequence,
                    "turn_index": turn_index,
                }
                if lease_is_current is not None and not lease_is_current():
                    raise RuntimeError("stale migration stage lease")
                self.midterm_memory.insert_page(page_id, page_payload)
            if source_job_id:
                pages.append(page_payload)
                previous_page_id = page_id
                continue
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
            self.midterm_memory.update_page(page_id, page_payload, reembed=False)
            pages.append(page_payload)
            previous_page_id = page_id

        return pages

    async def process_evicted_messages_async(
        self,
        evicted_messages: List[Dict[str, Any]],
        filters: Dict[str, Any],
        *,
        source_job_id: Optional[str] = None,
        lease_token: Optional[str] = None,
        lease_is_current=None,
        degraded: bool = False,
    ) -> List[Dict[str, Any]]:
        """Prepare pages in order while allowing separate jobs to overlap async LLM I/O."""
        scope_filters = self._scope_filters(filters)
        if not evicted_messages or not scope_filters:
            return []

        qa_pairs = self._messages_to_qa_pairs(evicted_messages)
        turn_indices = self._turn_indices(qa_pairs)
        page_ids = [
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:midterm:{source_job_id}:{index}"))
            if source_job_id
            else str(uuid.uuid4())
            for index in range(len(qa_pairs))
        ]
        existing_rows = (
            await asyncio.gather(*(asyncio.to_thread(self.midterm_memory.get_page, page_id) for page_id in page_ids))
            if source_job_id
            else [None] * len(page_ids)
        )
        existing_pages = dict(zip(page_ids, existing_rows))
        sequences_to_reserve = sum(
            not existing_page or (getattr(existing_page, "payload", None) or {}).get("page_sequence") is None
            for existing_page in existing_pages.values()
        )
        reserved_sequences = iter(
            await asyncio.to_thread(
                self.midterm_memory.reserve_page_sequences,
                scope_filters,
                sequences_to_reserve,
            )
        )
        pages = []
        previous_page_id = await asyncio.to_thread(self._latest_page_id, scope_filters)
        for index, qa_pair in enumerate(qa_pairs):
            if lease_is_current is not None and not lease_is_current():
                raise RuntimeError("stale migration stage lease")
            page_id = page_ids[index]
            turn_index = turn_indices[index]
            now = beijing_now_iso()
            raw_dialogue = _format_page_dialogue(
                qa_pair.get("user_input", ""),
                qa_pair.get("assistant_response", ""),
            )
            existing_page = existing_pages.get(page_id)
            page_sequence = (getattr(existing_page, "payload", None) or {}).get("page_sequence")
            if page_sequence is None:
                page_sequence = next(reserved_sequences)
            if existing_page:
                page_payload = dict(getattr(existing_page, "payload", None) or {})
                page_payload.setdefault("id", page_id)
                page_payload.setdefault("page_sequence", page_sequence)
                if int(page_payload["turn_index"]) != turn_index:
                    raise RuntimeError("existing Mid-term Page turn_index does not match its source messages")
                if source_job_id:
                    page_payload.update(
                        {
                            "source_stage": "midterm",
                            "output_state": "staging",
                            "output_lease_token": lease_token,
                            "degraded": degraded,
                            "needs_reprocessing": degraded,
                            "updated_at": now,
                        }
                    )
                    if lease_is_current is not None and not lease_is_current():
                        raise RuntimeError("stale migration stage lease")
                    await asyncio.to_thread(
                        self.midterm_memory.update_page,
                        page_id,
                        page_payload,
                        reembed=False,
                    )
                    await asyncio.to_thread(
                        self._take_over_staging_session_output,
                        page_payload,
                        source_job_id,
                        lease_token,
                        lease_is_current,
                    )
                    pages.append(page_payload)
                    previous_page_id = page_id
                    continue
                if page_payload.get("session_id"):
                    pages.append(page_payload)
                    previous_page_id = page_id
                    continue
                if degraded:
                    page_payload["degraded"] = True
                    page_payload["needs_reprocessing"] = True
                    page_payload["updated_at"] = now
                    await asyncio.to_thread(
                        self.midterm_memory.update_page,
                        page_id,
                        page_payload,
                        reembed=False,
                    )
            else:
                if degraded:
                    summary = qa_pair.get("user_input", "").strip() or raw_dialogue[:240]
                    keywords = self._fallback_keywords(raw_dialogue)
                else:
                    previous_raw_dialogues, following_raw_dialogues = await asyncio.gather(
                        asyncio.to_thread(self._previous_raw_dialogues, previous_page_id),
                        asyncio.to_thread(self._following_raw_dialogues, scope_filters, turn_index),
                    )
                    summary, keywords = await self._summarize_page_async(
                        qa_pair.get("user_input", ""),
                        qa_pair.get("assistant_response", ""),
                        previous_raw_dialogues=previous_raw_dialogues,
                        following_raw_dialogues=following_raw_dialogues,
                        allow_fallback=source_job_id is None,
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
                    "source_stage": "midterm" if source_job_id else None,
                    "output_state": "staging" if source_job_id else "committed",
                    "output_lease_token": lease_token,
                    "degraded": degraded,
                    "needs_reprocessing": degraded,
                    "valid_recall_count": 0,
                    "last_recall_at": None,
                    "last_recall_turn_index": None,
                    "page_sequence": page_sequence,
                    "turn_index": turn_index,
                }
                if lease_is_current is not None and not lease_is_current():
                    raise RuntimeError("stale migration stage lease")
                await asyncio.to_thread(self.midterm_memory.insert_page, page_id, page_payload)
            if source_job_id:
                pages.append(page_payload)
                previous_page_id = page_id
                continue
            await asyncio.to_thread(self._link_previous_page, previous_page_id, page_id)

            new_session_id = (
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:midterm-session:{source_job_id}:{index}"))
                if source_job_id
                else None
            )
            session_id = await asyncio.to_thread(self._session_id_for_page, page_id, scope_filters)
            if session_id is None:
                session_id = await self._assign_session_async(
                    page_payload,
                    scope_filters,
                    allow_fallback=source_job_id is None,
                    use_llm=not degraded,
                    new_session_id=new_session_id,
                )
            page_payload["session_id"] = session_id
            page_payload["updated_at"] = beijing_now_iso()
            await asyncio.to_thread(
                self.midterm_memory.update_page,
                page_id,
                page_payload,
                reembed=False,
            )
            pages.append(page_payload)
            previous_page_id = page_id

        return pages

    def commit_source_job_outputs(
        self,
        source_job_id: str,
        lease_token: str,
        *,
        degraded: bool,
        lease_is_current,
    ) -> None:
        """Publish all prepared pages only while the caller still owns the stage."""
        rows = self.midterm_memory.list_pages(top_k=10000, include_uncommitted=True)
        rows = [row for row in rows if (getattr(row, "payload", None) or {}).get("source_job_id") == source_job_id]
        rows.sort(key=lambda row: (getattr(row, "payload", None) or {}).get("created_at") or "")
        for row in rows:
            if not lease_is_current():
                raise RuntimeError("stale migration stage lease")
            page_id = str(row.id)
            page_payload = dict(getattr(row, "payload", None) or {})
            if page_payload.get("output_state") == "committed":
                continue
            if page_payload.get("output_lease_token") != lease_token:
                raise RuntimeError("staging output is owned by another lease")
            scope_filters = self._scope_filters(page_payload)
            session_id = self._session_id_for_page(
                page_id,
                scope_filters,
                include_uncommitted=True,
            )
            if session_id is None:
                session_id = self._assign_session(
                    page_payload,
                    scope_filters,
                    allow_fallback=False,
                    use_llm=not degraded,
                    new_session_id=str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:midterm-session:{source_job_id}:{page_id}")
                    ),
                    include_uncommitted=True,
                )
            session = self.midterm_memory.get_session(session_id)
            if session:
                session_payload = dict(getattr(session, "payload", None) or {})
                if (
                    session_payload.get("output_state") == "staging"
                    and session_payload.get("created_by_source_job_id") == source_job_id
                    and session_payload.get("created_by_lease_token") == lease_token
                    and session_payload.get("output_lease_token") == lease_token
                ):
                    if not lease_is_current():
                        raise RuntimeError("stale migration stage lease")
                    session_payload["output_state"] = "committed"
                    session_payload["output_lease_token"] = None
                    self.midterm_memory.update_session(session_id, session_payload, reembed=False)
            if not lease_is_current():
                raise RuntimeError("stale migration stage lease")
            page_payload["session_id"] = session_id
            page_payload["output_state"] = "committed"
            page_payload["output_lease_token"] = None
            page_payload["degraded"] = degraded
            page_payload["needs_reprocessing"] = degraded
            page_payload.pop("_commit_session_backup", None)
            page_payload.pop("_commit_session_id", None)
            self.midterm_memory.update_page(page_id, page_payload, reembed=False)

    async def commit_source_job_outputs_async(
        self,
        source_job_id: str,
        lease_token: str,
        *,
        degraded: bool,
        lease_is_current,
    ) -> None:
        """Async commit path whose session merge uses native async LLM I/O."""
        rows = await asyncio.to_thread(
            self.midterm_memory.list_pages,
            top_k=10000,
            include_uncommitted=True,
        )
        rows = [row for row in rows if (getattr(row, "payload", None) or {}).get("source_job_id") == source_job_id]
        rows.sort(key=lambda row: (getattr(row, "payload", None) or {}).get("created_at") or "")
        for row in rows:
            if not lease_is_current():
                raise RuntimeError("stale migration stage lease")
            page_id = str(row.id)
            page_payload = dict(getattr(row, "payload", None) or {})
            if page_payload.get("output_state") == "committed":
                continue
            if page_payload.get("output_lease_token") != lease_token:
                raise RuntimeError("staging output is owned by another lease")
            scope_filters = self._scope_filters(page_payload)
            session_id = await asyncio.to_thread(
                self._session_id_for_page,
                page_id,
                scope_filters,
                include_uncommitted=True,
            )
            if session_id is None:
                session_id = await self._assign_session_async(
                    page_payload,
                    scope_filters,
                    allow_fallback=False,
                    use_llm=not degraded,
                    new_session_id=str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:midterm-session:{source_job_id}:{page_id}")
                    ),
                    include_uncommitted=True,
                )
            session = await asyncio.to_thread(self.midterm_memory.get_session, session_id)
            if session:
                session_payload = dict(getattr(session, "payload", None) or {})
                if (
                    session_payload.get("output_state") == "staging"
                    and session_payload.get("created_by_source_job_id") == source_job_id
                    and session_payload.get("created_by_lease_token") == lease_token
                    and session_payload.get("output_lease_token") == lease_token
                ):
                    if not lease_is_current():
                        raise RuntimeError("stale migration stage lease")
                    session_payload["output_state"] = "committed"
                    session_payload["output_lease_token"] = None
                    await asyncio.to_thread(
                        self.midterm_memory.update_session,
                        session_id,
                        session_payload,
                        reembed=False,
                    )
            if not lease_is_current():
                raise RuntimeError("stale migration stage lease")
            page_payload["session_id"] = session_id
            page_payload["output_state"] = "committed"
            page_payload["output_lease_token"] = None
            page_payload["degraded"] = degraded
            page_payload["needs_reprocessing"] = degraded
            page_payload.pop("_commit_session_backup", None)
            page_payload.pop("_commit_session_id", None)
            await asyncio.to_thread(
                self.midterm_memory.update_page,
                page_id,
                page_payload,
                reembed=False,
            )

    def discard_source_job_outputs(self, source_job_id: str, lease_token: str) -> Optional[str]:
        """Hide first, then best-effort restore/delete session side effects."""
        cleanup_errors = []
        rows = self.midterm_memory.list_pages(top_k=10000, include_uncommitted=True)
        rows = [row for row in rows if (getattr(row, "payload", None) or {}).get("source_job_id") == source_job_id]
        for row in rows:
            page_payload = dict(getattr(row, "payload", None) or {})
            if page_payload.get("output_state") != "staging":
                continue
            if page_payload.get("output_lease_token") != lease_token:
                continue
            page_payload["output_state"] = "discarded"
            page_payload["output_lease_token"] = None
            try:
                self.midterm_memory.update_page(str(row.id), page_payload, reembed=False)
            except Exception as exc:
                cleanup_errors.append(f"page {row.id}: {exc}")
                continue
            session_id = page_payload.get("_commit_session_id") or page_payload.get("session_id")
            if not session_id:
                continue
            try:
                session = self.midterm_memory.get_session(session_id)
                session_payload = dict(getattr(session, "payload", None) or {}) if session else {}
                if (
                    session_payload.get("output_state") == "staging"
                    and session_payload.get("output_lease_token") == lease_token
                    and session_payload.get("created_by_source_job_id") == source_job_id
                    and session_payload.get("created_by_lease_token") == lease_token
                ):
                    session_payload["output_state"] = "discarded"
                    session_payload["output_lease_token"] = None
                    self.midterm_memory.update_session(session_id, session_payload, reembed=False)
                elif (
                    page_payload.get("_commit_session_backup") is not None
                    and session_payload.get("last_output_job_id") == source_job_id
                    and session_payload.get("last_output_lease_token") == lease_token
                ):
                    self.midterm_memory.update_session(
                        session_id,
                        page_payload["_commit_session_backup"],
                        reembed=True,
                    )
            except Exception as exc:
                cleanup_errors.append(f"session {session_id}: {exc}")
        return "; ".join(cleanup_errors) or None

    def promote_hot_sessions(self) -> List[Dict[str, Any]]:
        hot_sessions = []
        for session in self.midterm_memory.list_sessions(top_k=10000):
            payload = dict(getattr(session, "payload", None) or {})
            scope_filters = self._scope_filters(payload)
            current_turn_index = self.midterm_memory.current_turn_index(scope_filters)
            payload["R_recency"] = compute_recency(
                int(payload["last_visit_turn_index"]),
                current_turn_index,
                self.config.heat_recency_tau_turns,
            )
            payload["H_segment"] = compute_session_heat(payload, self.config, current_turn_index)
            if payload["H_segment"] >= self.config.promotion_heat_threshold:
                hot_sessions.append({"id": str(session.id), **payload})
        return hot_sessions

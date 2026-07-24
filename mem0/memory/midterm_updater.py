import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT
from mem0.memory.midterm import compute_session_heat, keyword_overlap
from mem0.memory.utils import extract_json, remove_code_blocks
from mem0.utils.timestamps import (
    BEIJING_TIMEZONE,
    beijing_now_iso,
    normalize_iso_timestamp_to_beijing,
)

logger = logging.getLogger(__name__)


class MidTermUpdater:
    def __init__(self, midterm_memory, llm, config):
        self.midterm_memory = midterm_memory
        self.llm = llm
        self.config = config

    @staticmethod
    def _scope_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in (filters or {}).items()
            if key in ("user_id", "agent_id", "run_id") and value
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

    def _summarize_page(self, user_input: str, assistant_response: str) -> tuple[str, List[str]]:
        raw_dialogue = f"User: {user_input}\nAssistant: {assistant_response}".strip()
        try:
            response = self.llm.generate_response(
                messages=[
                    {"role": "system", "content": MIDTERM_PAGE_SUMMARY_PROMPT},
                    {"role": "user", "content": raw_dialogue},
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
        except Exception as exc:
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
        except Exception as exc:
            logger.debug("Midterm session merge failed; using fallback: %s", exc)

        return fallback

    def _append_page_to_session(self, session_id: str, page_payload: Dict[str, Any]) -> str:
        session = self.midterm_memory.get_session(session_id)
        if not session:
            return self._create_session(page_payload)

        payload = dict(getattr(session, "payload", None) or {})
        page_ids = list(payload.get("page_ids") or [])
        if page_payload["id"] not in page_ids:
            page_ids.append(page_payload["id"])

        summary, keywords = self._merge_session(
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
        payload["R_recency"] = float(payload.get("R_recency", 1.0) or 1.0)
        payload["H_segment"] = compute_session_heat(payload, self.config)
        self.midterm_memory.update_session(session_id, payload, reembed=True)
        return session_id

    def _create_session(self, page_payload: Dict[str, Any]) -> str:
        now = beijing_now_iso()
        session_id = str(uuid.uuid4())
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
        }
        payload["H_segment"] = compute_session_heat(payload, self.config)
        self.midterm_memory.insert_session(session_id, payload)
        return session_id

    def _assign_session(self, page_payload: Dict[str, Any], filters: Dict[str, Any]) -> str:
        query = self.midterm_memory.page_embedding_text(page_payload)
        candidate_sessions = self.midterm_memory.search_sessions(
            query=query,
            filters=filters,
            top_k=self.config.top_k_sessions,
        )

        best_session_id = None
        best_score = -1.0
        for session in candidate_sessions:
            payload = getattr(session, "payload", None) or {}
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
            return self._append_page_to_session(best_session_id, page_payload)
        return self._create_session(page_payload)

    def process_evicted_messages(
        self,
        evicted_messages: List[Dict[str, Any]],
        filters: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        scope_filters = self._scope_filters(filters)
        if not evicted_messages or not scope_filters:
            return []

        pages = []
        previous_page_id = self._latest_page_id(scope_filters)
        for qa_pair in self._messages_to_qa_pairs(evicted_messages):
            page_id = str(uuid.uuid4())
            now = beijing_now_iso()
            summary, keywords = self._summarize_page(
                qa_pair.get("user_input", ""),
                qa_pair.get("assistant_response", ""),
            )
            raw_dialogue = (
                f"User: {qa_pair.get('user_input', '')}\n"
                f"Assistant: {qa_pair.get('assistant_response', '')}"
            ).strip()
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
            }
            self.midterm_memory.insert_page(page_id, page_payload)
            self._link_previous_page(previous_page_id, page_id)

            session_id = self._assign_session(page_payload, scope_filters)
            page_payload["session_id"] = session_id
            page_payload["updated_at"] = beijing_now_iso()
            self.midterm_memory.update_page(page_id, page_payload, reembed=False)
            pages.append(page_payload)
            previous_page_id = page_id

        return pages

    def promote_hot_sessions(self) -> List[Dict[str, Any]]:
        hot_sessions = []
        for session in self.midterm_memory.list_sessions(top_k=10000):
            payload = getattr(session, "payload", None) or {}
            if float(payload.get("H_segment", 0.0) or 0.0) >= self.config.promotion_heat_threshold:
                hot_sessions.append({"id": str(session.id), **payload})
        return hot_sessions

import asyncio
import json
import math
import threading
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from mem0 import Memory
from mem0.configs.base import MemoryConfig, MidTermMemoryConfig
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT
from mem0.memory.midterm_retriever import MidTermRetriever
from mem0.memory.midterm_updater import MidTermUpdater
from mem0.memory.memory_evolution import forgetting_factor, heat_modulations, memory_strength
from mem0.memory.storage import SQLiteManager
from mem0.utils.timestamps import beijing_now


class FakeEmbedding:
    dims = 8

    def embed(self, text, memory_action=None):
        text = (text or "").lower()
        vector = [0.05] * self.dims
        buckets = [
            ["风险", "亏损", "10%", "保守", "risk", "loss"],
            ["中长期", "短线", "风格"],
            ["新能源", "行业", "产业链"],
            ["债券", "基金", "配置"],
        ]
        for index, terms in enumerate(buckets):
            if any(term in text for term in terms):
                vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_batch(self, texts, memory_action="add"):
        return [self.embed(text, memory_action) for text in texts]


class FakeLLM:
    def generate_response(self, messages, response_format=None, **kwargs):
        system = messages[0]["content"] if messages else ""
        user_prompt = messages[-1]["content"] if messages else ""
        if system == MIDTERM_PAGE_SUMMARY_PROMPT:
            return json.dumps(self._page_summary(user_prompt), ensure_ascii=False)
        if system == MIDTERM_SESSION_MERGE_PROMPT:
            payload = json.loads(user_prompt)
            existing = payload.get("existing_session", {})
            new_page = payload.get("new_page", {})
            summary = " ".join(part for part in [existing.get("summary", ""), new_page.get("summary", "")] if part)
            keywords = []
            for keyword in existing.get("keywords", []) + new_page.get("keywords", []):
                if keyword not in keywords:
                    keywords.append(keyword)
            return json.dumps({"summary": summary[:1000], "keywords": keywords[:12]}, ensure_ascii=False)
        if "用户画像描述用户本人" in system:
            return json.dumps({"operations": [], "unmapped_facts": []}, ensure_ascii=False)

        memory = self._fact_memory(user_prompt)
        return json.dumps({"memory": [{"text": memory}]} if memory else {"memory": []}, ensure_ascii=False)

    @staticmethod
    def _page_summary(text):
        if "10%" in text or "亏损" in text or "风险" in text:
            return {"summary": "用户风险偏好保守，最大亏损为10%。", "keywords": ["风险", "亏损", "10%"]}
        if "中长期" in text or "短线" in text:
            return {"summary": "用户偏好中长期投资，不喜欢短线。", "keywords": ["投资风格", "中长期", "短线"]}
        if "新能源" in text:
            return {"summary": "用户关注新能源车产业链。", "keywords": ["新能源", "行业", "产业链"]}
        return {"summary": text[:120], "keywords": ["投资"]}

    @staticmethod
    def _fact_memory(text):
        if "10%" in text or "亏损" in text or "风险" in text:
            return "用户风险偏好保守，最大可接受亏损为10%。"
        if "中长期" in text or "短线" in text:
            return "用户偏好中长期投资，不喜欢短线交易。"
        if "新能源" in text:
            return "用户关注新能源车产业链。"
        if "债券基金" in text or "基金配置" in text:
            return "用户偏好较高比例配置债券基金。"
        return None


class RecordingFakeLLM(FakeLLM):
    def __init__(self):
        self.calls = []

    def generate_response(self, messages, response_format=None, **kwargs):
        self.calls.append({"messages": messages, "response_format": response_format, **kwargs})
        return super().generate_response(messages, response_format=response_format, **kwargs)


def test_midterm_prompts_are_chinese_and_preserve_json_contract():
    assert "只返回严格有效的 JSON 对象" in MIDTERM_PAGE_SUMMARY_PROMPT
    assert "用户提供的关键数据、假设和限制" in MIDTERM_PAGE_SUMMARY_PROMPT
    assert "summary" in MIDTERM_PAGE_SUMMARY_PROMPT
    assert "keywords" in MIDTERM_PAGE_SUMMARY_PROMPT
    assert "只返回严格有效的 JSON 对象" in MIDTERM_SESSION_MERGE_PROMPT
    assert "整个 Session 当前状态" in MIDTERM_SESSION_MERGE_PROMPT
    assert "summary" in MIDTERM_SESSION_MERGE_PROMPT
    assert "keywords" in MIDTERM_SESSION_MERGE_PROMPT


def test_midterm_summary_request_and_persisted_dialogue_share_real_blank_line_separator(
    tmp_path,
    fake_memory_env,
):
    memory = Memory(_memory_config(tmp_path, collection_name="midterm_dialogue_format"))
    llm = RecordingFakeLLM()
    memory.midterm_updater.llm = llm
    messages = [
        {"role": "user", "content": '用户第一行\n用户第二行，含引号 " 和 emoji 😀', "turn_index": 1},
        {"role": "assistant", "content": "助手第一行\n助手第二行，含反斜杠 \\", "turn_index": 1},
    ]
    expected = 'User: 用户第一行\n用户第二行，含引号 " 和 emoji 😀\n\nAssistant: 助手第一行\n助手第二行，含反斜杠 \\'

    try:
        pages = memory.midterm_updater.process_evicted_messages(
            messages,
            {"user_id": "u1", "run_id": "r1"},
        )
        summary_call = next(call for call in llm.calls if call["messages"][0]["content"] == MIDTERM_PAGE_SUMMARY_PROMPT)

        summary_input = summary_call["messages"][1]["content"]
        assert "上文：\n\n（无）" in summary_input
        assert f"当前待总结对话：\n\n{expected}" in summary_input
        assert "下文：\n\n（无）" in summary_input
        assert pages[0]["raw_dialogue"] == expected
        assert pages[0]["page_sequence"] == 1
        assert pages[0]["turn_index"] == 1
        assert "User: " in expected
        assert "\n\nAssistant: " in expected
        assert "\\n\\nAssistant" not in expected
    finally:
        memory.close()


class InMemoryVectorStore:
    def __init__(self, name):
        self.name = name
        self.rows = {}

    def insert(self, vectors, payloads=None, ids=None):
        for index, payload in enumerate(payloads or []):
            vector_id = ids[index]
            self.rows[vector_id] = {
                "vector": vectors[index] if vectors else None,
                "payload": dict(payload or {}),
            }

    def search(self, query, vectors, top_k=5, filters=None):
        rows = [self._row(vector_id, row, vectors) for vector_id, row in self.rows.items()]
        rows = [row for row in rows if self._matches(row.payload, filters)]
        rows.sort(key=lambda row: row.score, reverse=True)
        return rows[:top_k]

    def list(self, filters=None, top_k=None):
        rows = [
            SimpleNamespace(id=vector_id, payload=dict(row["payload"]), score=1.0)
            for vector_id, row in self.rows.items()
            if self._matches(row["payload"], filters)
        ]
        return rows if top_k is None else rows[:top_k]

    def get(self, vector_id):
        row = self.rows.get(vector_id)
        if not row:
            return None
        return SimpleNamespace(id=vector_id, payload=dict(row["payload"]), score=1.0)

    def update(self, vector_id, vector=None, payload=None):
        if vector_id not in self.rows:
            return
        if vector is not None:
            self.rows[vector_id]["vector"] = vector
        if payload is not None:
            self.rows[vector_id]["payload"] = dict(payload)

    def delete(self, vector_id):
        self.rows.pop(vector_id, None)

    def delete_col(self):
        self.rows.clear()

    def reset(self):
        self.rows.clear()

    def keyword_search(self, query, top_k=5, filters=None):
        return []

    @staticmethod
    def _matches(payload, filters):
        for key, expected in (filters or {}).items():
            if payload.get(key) != expected:
                return False
        return True

    @staticmethod
    def _row(vector_id, row, query_vector):
        stored_vector = row.get("vector") or []
        score = sum(left * right for left, right in zip(stored_vector, query_vector or []))
        return SimpleNamespace(id=vector_id, payload=dict(row["payload"]), score=score)


@pytest.fixture
def fake_memory_env(monkeypatch):
    stores = {}

    def create_vector_store(provider, config, **kwargs):
        collection_name = getattr(config, "collection_name", None)
        if collection_name is None and isinstance(config, dict):
            collection_name = config.get("collection_name")
        collection_name = collection_name or "mem0"
        stores.setdefault(collection_name, InMemoryVectorStore(collection_name))
        return stores[collection_name]

    monkeypatch.setattr("mem0.memory.main.MEM0_TELEMETRY", False)
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *args, **kwargs: None)
    monkeypatch.setattr("mem0.memory.main.EmbedderFactory.create", lambda *args, **kwargs: FakeEmbedding())
    monkeypatch.setattr("mem0.memory.main.LlmFactory.create", lambda *args, **kwargs: FakeLLM())
    monkeypatch.setattr("mem0.memory.main.VectorStoreFactory.create", create_vector_store)
    monkeypatch.setattr("mem0.memory.main.VectorStoreFactory.reset", lambda store: store.reset() or store)
    monkeypatch.setattr("mem0.memory.midterm.VectorStoreFactory.create", create_vector_store)
    monkeypatch.setattr("mem0.memory.cross_session_longterm.VectorStoreFactory.create", create_vector_store)
    monkeypatch.setattr("mem0.memory.main.extract_entities", lambda *args, **kwargs: [])
    monkeypatch.setattr("mem0.memory.main.extract_entities_batch", lambda *args, **kwargs: [])
    return stores


def _memory_config(tmp_path, *, enabled=True, collection_name="midterm_test"):
    config = MemoryConfig()
    config.history_db_path = str(tmp_path / f"{collection_name}.db")
    config.vector_store.config.collection_name = collection_name
    config.midterm.enabled = enabled
    config.midterm.short_term_capacity = 2
    config.midterm.session_similarity_threshold = 0.5
    config.midterm.top_k_sessions = 5
    config.midterm.top_k_pages = 5
    return config


def test_midterm_config_defaults():
    config = MemoryConfig()
    assert config.midterm.enabled is True
    assert config.midterm.short_term_capacity == 10
    assert config.midterm.max_total_pages == 4
    assert config.midterm.midterm_rag_threshold == 0.1
    assert config.longterm_rag_threshold == 0.1
    assert config.cross_session_longterm_rag_threshold == 0.1
    assert config.cross_session_retention_half_life_hours == 720.0
    assert config.cross_session_retention_floor == 0.2
    assert config.cross_session_reinforcement_gain == 0.25
    assert config.midterm.retention_half_life_turns == 168.0
    assert config.midterm.heat_recency_tau_turns == 24.0
    assert config.midterm.retention_floor == 0.2
    assert not hasattr(config.midterm, "reinforcement_gain")
    assert config.midterm.heat_modulation_min == 0.9
    assert config.midterm.heat_modulation_max == 1.1
    assert config.midterm.promotion_min_recall_count == 3
    assert config.midterm.promotion_heat_threshold == 5.0


def test_midterm_config_rejects_negative_page_limits():
    with pytest.raises(ValueError):
        MidTermMemoryConfig(top_k_sessions=-1)
    with pytest.raises(ValueError):
        MidTermMemoryConfig(top_k_pages=-1)
    with pytest.raises(ValueError):
        MidTermMemoryConfig(max_total_pages=-1)


def _midterm_row(row_id, score, **payload):
    if "session_id" in payload:
        payload.setdefault("turn_index", 1)
    else:
        payload.setdefault("N_visit", payload.get("H_segment", 0))
        payload.setdefault("L_interaction", 0 if "H_segment" in payload else 1)
        payload.setdefault("created_turn_index", 1)
        payload.setdefault("last_visit_turn_index", 1)
    return SimpleNamespace(id=row_id, score=score, payload=payload)


class FakeMidTermRetrievalStore:
    def __init__(self, sessions, pages_by_session, *, current_turn_index=1):
        self.sessions = sessions
        self.pages_by_session = pages_by_session
        self.session_filters = []
        self.page_filters = []
        self.visited_sessions = []
        self.valid_recalled_pages = []
        self.current_turn = current_turn_index

    def search_sessions(self, query, filters=None, top_k=5):
        self.session_filters.append(dict(filters or {}))
        rows = [row for row in self.sessions if self._matches(row.payload, filters)]
        return rows[:top_k]

    def search_pages(self, query, filters=None, top_k=5):
        self.page_filters.append({"filters": dict(filters or {}), "top_k": top_k})
        session_id = (filters or {}).get("session_id")
        rows = self.pages_by_session.get(session_id, [])
        rows = [row for row in rows if self._matches(row.payload, filters)]
        return rows[:top_k]

    def record_session_visit(self, session_id):
        self.visited_sessions.append(session_id)

    def record_valid_recalls(self, page_ids, *, recall_turn_index):
        self.valid_recalled_pages.extend(page_ids)

    def current_turn_index(self, filters):
        return self.current_turn

    def get_session(self, session_id):
        return next((session for session in self.sessions if str(session.id) == str(session_id)), None)

    @staticmethod
    def _matches(payload, filters):
        return all(payload.get(key) == value for key, value in (filters or {}).items())


def _retriever_config(*, top_k_sessions=5, top_k_pages=5, max_total_pages=4):
    return MidTermMemoryConfig(
        enabled=True,
        top_k_sessions=top_k_sessions,
        top_k_pages=top_k_pages,
        max_total_pages=max_total_pages,
    )


def _page_results(results):
    return [item for item in results if item.get("source") == "mid_term_page"]


def test_midterm_retriever_applies_global_page_limit_and_score_order():
    sessions = [
        _midterm_row(f"s{session_index}", 1.0, summary=f"session {session_index}", user_id="u1", run_id="r1")
        for session_index in range(1, 6)
    ]
    pages_by_session = {}
    for session_index in range(1, 6):
        session_id = f"s{session_index}"
        pages_by_session[session_id] = [
            _midterm_row(
                f"{session_id}-p{page_index}",
                session_index * 10 + page_index,
                session_id=session_id,
                summary=f"page {session_index}-{page_index}",
                user_id="u1",
                run_id="r1",
            )
            for page_index in range(1, 6)
        ]
    store = FakeMidTermRetrievalStore(sessions, pages_by_session)
    retriever = MidTermRetriever(store, _retriever_config(top_k_sessions=5, top_k_pages=5, max_total_pages=4))

    results = retriever.search("risk", {"user_id": "u1", "run_id": "r1"})
    pages = _page_results(results)

    assert len(pages) == 4
    assert [page["score"] for page in pages] == [55.0, 54.0, 53.0, 52.0]
    assert [page["id"] for page in pages] == ["s5-p5", "s5-p4", "s5-p3", "s5-p2"]


def test_midterm_retriever_dedupes_same_page_from_multiple_sessions():
    sessions = [
        _midterm_row("s1", 0.9, summary="session 1", user_id="u1", run_id="r1"),
        _midterm_row("s2", 0.8, summary="session 2", user_id="u1", run_id="r1"),
    ]
    pages_by_session = {
        "s1": [
            _midterm_row("shared-page", 0.5, session_id="s1", summary="older", user_id="u1", run_id="r1"),
            _midterm_row("s1-p1", 0.4, session_id="s1", summary="s1 page", user_id="u1", run_id="r1"),
        ],
        "s2": [
            _midterm_row("shared-page", 0.9, session_id="s2", summary="newer", user_id="u1", run_id="r1"),
            _midterm_row("s2-p1", 0.8, session_id="s2", summary="s2 page", user_id="u1", run_id="r1"),
        ],
    }
    store = FakeMidTermRetrievalStore(sessions, pages_by_session)
    retriever = MidTermRetriever(store, _retriever_config(max_total_pages=5))

    pages = _page_results(retriever.search("risk", {"user_id": "u1", "run_id": "r1"}))

    assert [page["id"] for page in pages] == ["shared-page", "s2-p1", "s1-p1"]
    assert pages[0]["score"] == 0.9
    assert pages[0]["session_id"] == "s2"


def test_midterm_retriever_returns_all_pages_when_candidates_below_limit():
    sessions = [_midterm_row("s1", 0.9, summary="session 1", user_id="u1", run_id="r1")]
    pages_by_session = {
        "s1": [
            _midterm_row("p1", 0.7, session_id="s1", summary="page 1", user_id="u1", run_id="r1"),
            _midterm_row("p2", 0.6, session_id="s1", summary="page 2", user_id="u1", run_id="r1"),
        ],
    }
    store = FakeMidTermRetrievalStore(sessions, pages_by_session)
    retriever = MidTermRetriever(store, _retriever_config(max_total_pages=4))

    pages = _page_results(retriever.search("risk", {"user_id": "u1", "run_id": "r1"}))

    assert [page["id"] for page in pages] == ["p1", "p2"]


def test_midterm_production_config_retains_historical_page_budget_range():
    assert _retriever_config(max_total_pages=0).max_total_pages == 0
    assert _retriever_config(max_total_pages=6).max_total_pages == 6


def test_midterm_retriever_preserves_run_id_isolation():
    sessions = [
        _midterm_row("s1", 0.9, summary="run 1 session", user_id="u1", run_id="r1"),
        _midterm_row("s2", 0.8, summary="run 2 session", user_id="u1", run_id="r2"),
    ]
    pages_by_session = {
        "s1": [_midterm_row("p1", 0.7, session_id="s1", summary="run 1 page", user_id="u1", run_id="r1")],
        "s2": [_midterm_row("p2", 0.9, session_id="s2", summary="run 2 page", user_id="u1", run_id="r2")],
    }
    store = FakeMidTermRetrievalStore(sessions, pages_by_session)
    retriever = MidTermRetriever(store, _retriever_config())

    results = retriever.search("risk", {"user_id": "u1", "run_id": "r1"})

    assert [item["id"] for item in results] == ["s1", "p1"]
    assert store.session_filters == [{"user_id": "u1", "run_id": "r1"}]
    assert all(call["filters"]["run_id"] == "r1" for call in store.page_filters)


def test_midterm_retriever_concurrent_search_results_do_not_mix():
    class QueryScopedStore:
        def current_turn_index(self, filters):
            return 1

        def search_sessions(self, query, filters=None, top_k=5):
            return [
                _midterm_row(
                    f"{query}-session",
                    0.9,
                    summary=f"{query} session",
                    user_id=filters["user_id"],
                    run_id=filters["run_id"],
                )
            ]

        def search_pages(self, query, filters=None, top_k=5):
            return [
                _midterm_row(
                    f"{query}-page",
                    0.8,
                    session_id=filters["session_id"],
                    summary=f"{query} page",
                    user_id=filters["user_id"],
                    run_id=filters["run_id"],
                )
            ]

        def record_session_visit(self, session_id):
            return None

        def get_session(self, session_id):
            return None

    retriever = MidTermRetriever(QueryScopedStore(), _retriever_config())
    queries = ("risk", "allocation", "liquidity")

    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        results = list(
            pool.map(
                lambda query: retriever.search(query, {"user_id": "u1", "run_id": "r1"}),
                queries,
            )
        )

    for query, query_results in zip(queries, results):
        assert [item["id"] for item in query_results] == [f"{query}-session", f"{query}-page"]


def test_midterm_candidate_search_never_records_valid_recall():
    sessions = [_midterm_row("s1", 0.9, summary="session", user_id="u1", run_id="r1", H_segment=4.0)]
    pages_by_session = {
        "s1": [
            _midterm_row(
                "p1",
                0.8,
                session_id="s1",
                summary="page",
                raw_dialogue="User: q\n\nAssistant: a",
                user_id="u1",
                run_id="r1",
            )
        ]
    }
    store = FakeMidTermRetrievalStore(sessions, pages_by_session)

    results = MidTermRetriever(store, _retriever_config()).search(
        "risk",
        {"user_id": "u1", "run_id": "r1"},
    )

    assert [item["id"] for item in _page_results(results)] == ["p1"]
    assert store.valid_recalled_pages == []
    assert store.visited_sessions == []


def test_midterm_global_page_search_supplements_actual_unique_candidate_pool():
    sessions = [
        _midterm_row("s1", 0.9, summary="selected", user_id="u1", run_id="r1", H_segment=1.0),
        _midterm_row("s2", 0.8, summary="global", user_id="u1", run_id="r1", H_segment=1.0),
    ]
    local_pages = [
        _midterm_row("p1", 0.4, session_id="s1", summary="local 1", user_id="u1", run_id="r1"),
        _midterm_row("p2", 0.3, session_id="s1", summary="local 2", user_id="u1", run_id="r1"),
    ]
    global_pages = [
        *local_pages,
        *[
            _midterm_row(
                f"global-{index}",
                0.5 + index / 100,
                session_id="s2",
                summary=f"global {index}",
                user_id="u1",
                run_id="r1",
            )
            for index in range(1, 7)
        ],
    ]
    store = FakeMidTermRetrievalStore(sessions, {"s1": local_pages, None: global_pages})
    retriever = MidTermRetriever(
        store,
        _retriever_config(top_k_sessions=1, top_k_pages=2, max_total_pages=2),
    )

    pages = _page_results(retriever.search("risk", {"user_id": "u1", "run_id": "r1"}))

    assert len(pages) == 2
    assert [page["id"] for page in pages] == ["global-6", "global-5"]
    assert {call["top_k"] for call in store.page_filters if "session_id" not in call["filters"]} == {8}
    assert len({page.id for page in global_pages}) == 4 * 2


def test_midterm_threshold_uses_raw_score_not_heat_modulated_final_score():
    sessions = [
        _midterm_row("cold", 0.9, summary="cold", user_id="u1", run_id="r1", H_segment=0.0),
        _midterm_row("hot", 0.8, summary="hot", user_id="u1", run_id="r1", H_segment=10.0),
    ]
    pages_by_session = {
        "cold": [
            _midterm_row(
                "raw-pass",
                0.51,
                session_id="cold",
                summary="pass",
                user_id="u1",
                run_id="r1",
                page_sequence=1,
            )
        ],
        "hot": [
            _midterm_row(
                "final-pass-only",
                0.49,
                session_id="hot",
                summary="fail",
                user_id="u1",
                run_id="r1",
                page_sequence=1,
            )
        ],
    }
    config = _retriever_config(top_k_sessions=2, top_k_pages=1, max_total_pages=2)
    config.midterm_rag_threshold = 0.5
    store = FakeMidTermRetrievalStore(sessions, pages_by_session)

    pages = _page_results(MidTermRetriever(store, config).search("risk", {"user_id": "u1", "run_id": "r1"}))

    assert [page["id"] for page in pages] == ["raw-pass"]
    assert pages[0]["raw_rag_score"] == pytest.approx(0.51)
    assert config.heat_modulation_min < pages[0]["heat_factor"] < config.heat_modulation_max
    assert pages[0]["final_score"] == pytest.approx(pages[0]["raw_rag_score"] * pages[0]["forgetting_factor"])
    assert pages[0]["final_score"] == pytest.approx(0.51)


def test_midterm_forgetting_uses_page_distance_and_not_wall_clock():
    config = _retriever_config()
    config.retention_half_life_turns = 4.0
    config.retention_floor = 0.1
    now = beijing_now()
    near = {
        "turn_index": 8,
        "created_at": (now - timedelta(hours=24)).isoformat(),
    }
    far = {
        "turn_index": 4,
        "created_at": (now - timedelta(hours=48)).isoformat(),
    }
    same_distance_different_time = {
        "turn_index": 8,
        "created_at": (now - timedelta(days=365)).isoformat(),
    }

    near_retention = forgetting_factor(near, config, current_turn_index=12, now=now.isoformat())
    far_retention = forgetting_factor(far, config, current_turn_index=12, now=now.isoformat())
    wall_clock_independent = forgetting_factor(
        same_distance_different_time,
        config,
        current_turn_index=12,
        now=(now + timedelta(days=30)).isoformat(),
    )

    assert near_retention == pytest.approx(0.5)
    assert far_retention == pytest.approx(0.25)
    assert far_retention < near_retention
    assert wall_clock_independent == pytest.approx(near_retention)


def test_heat_maps_absolute_session_heat_to_bounded_half_life_factor():
    assert heat_modulations({"only": 7.0}, minimum=0.9, maximum=1.1) == {"only": pytest.approx(1.075)}
    equal = heat_modulations({"a": 2.0, "b": 2.0}, minimum=0.9, maximum=1.1)
    assert equal["a"] == pytest.approx(equal["b"])
    modulations = heat_modulations({"cold": 0.0, "warm": 5.0, "hot": 10.0}, minimum=0.8, maximum=1.2)
    assert modulations["cold"] == pytest.approx(0.8)
    assert 0.8 < modulations["warm"] < modulations["hot"] < 1.2

    config = _retriever_config()
    config.retention_half_life_turns = 10.0
    payload = {"turn_index": 0}
    cold_retention = forgetting_factor(
        payload,
        config,
        current_turn_index=10,
        heat_factor=modulations["cold"],
    )
    hot_retention = forgetting_factor(
        payload,
        config,
        current_turn_index=10,
        heat_factor=modulations["hot"],
    )
    assert hot_retention > cold_retention


def test_sqlite_save_messages_returns_natural_evictions():
    manager = SQLiteManager(":memory:")
    try:
        messages = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
        ]
        evicted = manager.save_messages(messages, "scope", max_messages=4, return_evicted=True)
        assert [message["role"] for message in evicted] == ["user"]
        assert [message["content"] for message in evicted] == ["u1"]
        assert [message["content"] for message in manager.get_last_messages("scope", limit=10)] == [
            "a1",
            "u2",
            "a2",
            "u3",
        ]
    finally:
        manager.close()


def test_messages_to_qa_pairs_tolerates_partial_pairs():
    pairs = MidTermUpdater._messages_to_qa_pairs(
        [
            {"role": "assistant", "content": "orphan assistant"},
            {"role": "user", "content": "u1", "turn_index": 1},
            {"role": "assistant", "content": "a1", "turn_index": 1},
            {"role": "user", "content": "u2", "turn_index": 2},
        ]
    )
    assert pairs == [
        {"user_input": "u1", "assistant_response": "a1", "created_at": None, "turn_index": 1},
        {"user_input": "u2", "assistant_response": "", "created_at": None, "turn_index": 2},
    ]
    assert MidTermUpdater._messages_to_qa_pairs([{"role": "assistant", "content": "assistant-only"}]) == []


def test_conversation_turn_index_is_transactional_and_independent_of_eviction(tmp_path):
    manager = SQLiteManager(str(tmp_path / "conversation-turns.db"))
    scope = "run_id=run-1&user_id=user-1"
    try:
        for index in range(1, 6):
            manager.save_messages(
                [
                    {"role": "user", "content": f"q{index}"},
                    {"role": "assistant", "content": f"a{index}"},
                ],
                scope,
                max_messages=6,
            )

        assert manager.current_turn_index(scope) == 5
        retained = manager.get_messages(scope, limit=10)
        assert [(message["content"], message["turn_index"]) for message in retained] == [
            ("q3", 3),
            ("a3", 3),
            ("q4", 4),
            ("a4", 4),
            ("q5", 5),
            ("a5", 5),
        ]
    finally:
        manager.close()


def test_concurrent_qa_writes_allocate_each_turn_once(tmp_path):
    manager = SQLiteManager(str(tmp_path / "concurrent-conversation-turns.db"))
    scope = "run_id=run-1&user_id=user-1"

    def save_turn(index):
        manager.save_messages(
            [
                {"role": "user", "content": f"q{index}"},
                {"role": "assistant", "content": f"a{index}"},
            ],
            scope,
            max_messages=100,
        )

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(save_turn, range(1, 13)))

        messages = manager.get_messages(scope, limit=100)
        turns = {}
        for message in messages:
            turns.setdefault(message["turn_index"], []).append(message["role"])
        assert manager.current_turn_index(scope) == 12
        assert sorted(turns) == list(range(1, 13))
        assert all(roles == ["user", "assistant"] for roles in turns.values())
    finally:
        manager.close()


def test_session_merge_uses_llm_and_bounds_keywords():
    class MergeLLM:
        def generate_response(self, messages, response_format=None, **kwargs):
            return json.dumps(
                {
                    "summary": "merged " * 300,
                    "keywords": [
                        "风险",
                        "风险",
                        "亏损",
                        "10%",
                        "中长期",
                        "短线",
                        "新能源",
                        "基金",
                        "债券",
                        "配置",
                        "行业",
                        "产业链",
                        "超额",
                    ],
                },
                ensure_ascii=False,
            )

    updater = MidTermUpdater(midterm_memory=None, llm=MergeLLM(), config=MidTermMemoryConfig())
    summary, keywords = updater._merge_session("old", ["风险"], "new", ["亏损"])
    assert len(summary) <= 1000
    assert summary.startswith("merged")
    assert keywords == [
        "风险",
        "亏损",
        "10%",
        "中长期",
        "短线",
        "新能源",
        "基金",
        "债券",
        "配置",
        "行业",
        "产业链",
        "超额",
    ]


@pytest.mark.asyncio
async def test_async_midterm_page_and_session_llm_calls_remain_ordered_within_job(tmp_path, fake_memory_env):
    class AsyncOnlyLLM:
        def __init__(self):
            self.active = 0
            self.maximum = 0
            self.calls = []

        def generate_response(self, **kwargs):
            raise AssertionError("sync LLM path must not be used")

        async def generate_response_async(self, messages, response_format=None, **kwargs):
            system = messages[0]["content"]
            kind = "page" if system == MIDTERM_PAGE_SUMMARY_PROMPT else "merge"
            self.calls.append(kind)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            if kind == "page":
                content = messages[-1]["content"]
                return json.dumps({"summary": content[:80], "keywords": [content.splitlines()[0]]})
            return json.dumps({"summary": "merged session", "keywords": ["merged"]})

    config = _memory_config(tmp_path, collection_name="async_midterm_order")
    config.midterm.session_similarity_threshold = -1
    memory = Memory(config)
    llm = AsyncOnlyLLM()
    memory.midterm_updater.llm = llm
    try:
        pages = await memory.midterm_updater.process_evicted_messages_async(
            [
                {"role": "user", "content": "first question", "turn_index": 1},
                {"role": "assistant", "content": "first answer", "turn_index": 1},
                {"role": "user", "content": "second question", "turn_index": 2},
                {"role": "assistant", "content": "second answer", "turn_index": 2},
            ],
            {"user_id": "u1", "run_id": "r1"},
        )
        assert len(pages) == 2
        assert llm.calls == ["page", "page", "merge"]
        assert llm.maximum == 1
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_async_midterm_llm_waits_can_overlap_between_jobs():
    class OverlapLLM:
        def __init__(self):
            self.active = 0
            self.maximum = 0

        async def generate_response_async(self, **kwargs):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            return json.dumps({"summary": "summary", "keywords": ["keyword"]})

    llm = OverlapLLM()
    updater = MidTermUpdater(midterm_memory=None, llm=llm, config=MidTermMemoryConfig())
    await asyncio.gather(*(updater._summarize_page_async(f"user-{index}", f"assistant-{index}") for index in range(8)))
    assert llm.maximum == 8


def test_memory_search_returns_long_and_midterm_sources(tmp_path, fake_memory_env):
    memory = Memory(_memory_config(tmp_path, collection_name="midterm_sources"))
    filters = {"user_id": "u1"}
    turns = [
        ("我比较保守，最大亏损最好控制在10%。", "我会按10%的最大亏损约束考虑。"),
        ("我偏中长期投资，不喜欢短线。", "后续建议会偏中长期。"),
    ]
    for user_message, assistant_message in turns:
        memory.add(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ],
            user_id="u1",
            infer=False,
        )

    assert memory.flush_background_tasks(5)
    assert memory.midterm_memory.list_pages(filters=filters, top_k=10)
    result = memory.search("我能接受多大亏损？", filters=filters, top_k=5)
    sources = {item.get("source") for item in result["results"]}
    assert {"long_term", "mid_term_session", "mid_term_page"}.issubset(sources)
    memory.close()


def test_midterm_partial_write_is_not_visible_before_commit(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="midterm_staging")
    config.background.enabled = False
    memory = Memory(config)
    messages = [
        {"role": "user", "content": "我偏好中长期投资。", "turn_index": 1},
        {"role": "assistant", "content": "我会记住。", "turn_index": 1},
    ]
    filters = {"user_id": "u1", "run_id": "r1"}

    memory._process_midterm_evictions(
        messages,
        filters,
        source_job_id="midterm-staging-job",
        lease_token="midterm-staging-lease",
        raise_on_error=True,
    )

    assert memory.midterm_memory.list_pages(filters=filters, top_k=10) == []
    staged = memory.midterm_memory.list_pages(
        filters=filters,
        top_k=10,
        include_uncommitted=True,
    )
    assert len(staged) == 1
    assert staged[0].payload["output_state"] == "staging"
    assert memory.midterm_retriever.search("中长期", filters) == []
    memory.close()


def test_midterm_retry_preserves_page_sequence_without_advancing_future_pages(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="midterm_retry_sequence")
    config.background.enabled = False
    memory = Memory(config)
    filters = {"user_id": "u1", "run_id": "r1"}
    messages = [
        {"role": "user", "content": "retry keeps order", "turn_index": 1},
        {"role": "assistant", "content": "ack", "turn_index": 1},
    ]

    first = memory.midterm_updater.process_evicted_messages(
        messages,
        filters,
        source_job_id="stable-sequence-job",
        lease_token="first-lease",
    )
    retried = memory.midterm_updater.process_evicted_messages(
        messages,
        filters,
        source_job_id="stable-sequence-job",
        lease_token="second-lease",
    )
    following = memory.midterm_updater.process_evicted_messages(messages, filters)

    assert first[0]["page_sequence"] == 1
    assert retried[0]["page_sequence"] == 1
    assert following[0]["page_sequence"] == 2
    assert first[0]["turn_index"] == 1
    assert retried[0]["turn_index"] == 1
    assert following[0]["turn_index"] == 1
    memory.close()


def test_discarded_midterm_does_not_pollute_existing_session_summary(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="midterm_discard")
    config.background.enabled = False
    memory = Memory(config)
    filters = {"user_id": "u1", "run_id": "r1"}
    memory._process_midterm_evictions(
        [
            {"role": "user", "content": "我比较保守，最大亏损10%。", "turn_index": 1},
            {"role": "assistant", "content": "我会按这个约束考虑。", "turn_index": 1},
        ],
        filters,
        raise_on_error=True,
    )
    before = {row.id: dict(row.payload) for row in memory.midterm_memory.list_sessions(filters=filters, top_k=10)}

    memory._process_midterm_evictions(
        [
            {"role": "user", "content": "这条内容不应进入现有摘要。", "turn_index": 2},
            {"role": "assistant", "content": "临时回答。", "turn_index": 2},
        ],
        filters,
        source_job_id="discarded-midterm-job",
        lease_token="discarded-midterm-lease",
        raise_on_error=True,
    )
    cleanup_error = memory.midterm_updater.discard_source_job_outputs(
        "discarded-midterm-job",
        "discarded-midterm-lease",
    )
    after = {row.id: dict(row.payload) for row in memory.midterm_memory.list_sessions(filters=filters, top_k=10)}
    staged = memory.midterm_memory.list_pages(
        filters=filters,
        top_k=10,
        include_uncommitted=True,
    )

    assert cleanup_error is None
    assert before == after
    assert any(row.payload.get("output_state") == "discarded" for row in staged)
    assert all(
        row.payload.get("source_job_id") != "discarded-midterm-job"
        for row in memory.midterm_memory.list_pages(filters=filters)
    )
    memory.close()


def test_old_midterm_lease_cannot_cleanup_new_lease_output(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="midterm_cleanup_fencing")
    config.background.enabled = False
    memory = Memory(config)
    filters = {"user_id": "u1", "run_id": "r1"}
    memory._process_midterm_evictions(
        [
            {"role": "user", "content": "新的租约应保留这条页面。", "turn_index": 1},
            {"role": "assistant", "content": "收到。", "turn_index": 1},
        ],
        filters,
        source_job_id="shared-job",
        lease_token="old-lease",
        raise_on_error=True,
    )
    row = memory.midterm_memory.list_pages(top_k=10, include_uncommitted=True)[0]
    payload = dict(row.payload)
    payload["output_lease_token"] = "new-lease"
    memory.midterm_memory.update_page(str(row.id), payload, reembed=False)
    session_id = memory.midterm_updater._create_session(payload)

    assert memory.midterm_updater.discard_source_job_outputs("shared-job", "old-lease") is None
    current_page = memory.midterm_memory.get_page(str(row.id)).payload
    current_session = memory.midterm_memory.get_session(session_id).payload
    assert current_page["output_state"] == "staging"
    assert current_page["output_lease_token"] == "new-lease"
    assert current_session["output_state"] == "staging"
    assert current_session["created_by_lease_token"] == "new-lease"
    memory.close()


def test_stale_midterm_cleanup_cannot_restore_new_session_state(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="midterm_session_cleanup_fencing")
    config.background.enabled = False
    memory = Memory(config)
    filters = {"user_id": "u1", "run_id": "r1"}
    memory._process_midterm_evictions(
        [
            {"role": "user", "content": "旧租约页面。", "turn_index": 1},
            {"role": "assistant", "content": "旧租约回答。", "turn_index": 1},
        ],
        filters,
        source_job_id="shared-job",
        lease_token="old-lease",
        raise_on_error=True,
    )
    row = memory.midterm_memory.list_pages(top_k=10, include_uncommitted=True)[0]
    page_payload = dict(row.payload)
    session_id = memory.midterm_updater._create_session(
        {
            **page_payload,
            "output_lease_token": "new-lease",
        }
    )
    session = memory.midterm_memory.get_session(session_id)
    session_payload = dict(session.payload)
    session_payload.update(
        {
            "summary": "new lease summary",
            "last_output_job_id": "shared-job",
            "last_output_lease_token": "new-lease",
        }
    )
    memory.midterm_memory.update_session(session_id, session_payload, reembed=False)
    page_payload.update(
        {
            "session_id": session_id,
            "_commit_session_id": session_id,
            "_commit_session_backup": {"summary": "old backup"},
        }
    )
    memory.midterm_memory.update_page(str(row.id), page_payload, reembed=False)

    memory.midterm_updater.discard_source_job_outputs("shared-job", "old-lease")
    current_session = memory.midterm_memory.get_session(session_id).payload
    assert current_session["summary"] == "new lease summary"
    assert current_session["last_output_lease_token"] == "new-lease"
    memory.close()


def test_stale_midterm_cleanup_cannot_modify_committed_page(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="midterm_committed_cleanup_fencing")
    config.background.enabled = False
    memory = Memory(config)
    memory._process_midterm_evictions(
        [
            {"role": "user", "content": "已提交页面。", "turn_index": 1},
            {"role": "assistant", "content": "不会被旧租约清理。", "turn_index": 1},
        ],
        {"user_id": "u1", "run_id": "r1"},
        source_job_id="shared-job",
        lease_token="new-lease",
        raise_on_error=True,
    )
    row = memory.midterm_memory.list_pages(top_k=10, include_uncommitted=True)[0]
    payload = dict(row.payload)
    payload["output_state"] = "committed"
    payload["output_lease_token"] = None
    memory.midterm_memory.update_page(str(row.id), payload, reembed=False)

    memory.midterm_updater.discard_source_job_outputs("shared-job", "old-lease")
    current = memory.midterm_memory.get_page(str(row.id)).payload
    assert current["output_state"] == "committed"
    assert current["output_lease_token"] is None
    memory.close()


def test_success_commits_all_midterm_stage_outputs(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="midterm_commit")
    config.background.enabled = False
    memory = Memory(config)
    messages = [
        {"role": "user", "content": "我偏好中长期投资。", "turn_index": 1},
        {"role": "assistant", "content": "我会记住。", "turn_index": 1},
    ]
    filters = {"user_id": "u1", "run_id": "r1"}
    job_id = memory.db.save_messages_and_create_migration_job(
        messages,
        "run_id=r1&user_id=u1",
        max_messages=0,
        filters=filters,
        metadata=filters,
        infer=True,
        prompt=None,
    )
    job = memory.db.claim_migration_stage(job_id, "midterm")

    memory._background_process_midterm(job, messages, False)
    assert memory.midterm_memory.list_pages(filters=filters, top_k=10) == []
    memory._commit_migration_stage_outputs(
        job,
        "midterm",
        job["midterm_lease_token"],
        False,
    )
    assert memory.db.mark_migration_stage_succeeded(
        job_id,
        "midterm",
        job["midterm_lease_token"],
    )

    pages = memory.midterm_memory.list_pages(filters=filters, top_k=10)
    sessions = memory.midterm_memory.list_sessions(filters=filters, top_k=10)
    assert pages and sessions
    assert all(page.payload["output_state"] == "committed" for page in pages)
    assert all(page.payload["degraded"] is False for page in pages)
    memory.close()


def test_memory_add_infer_true_updates_long_and_midterm(tmp_path, fake_memory_env):
    memory = Memory(_memory_config(tmp_path, collection_name="midterm_infer_true"))
    filters = {"user_id": "u1"}
    turns = [
        (
            "我比较保守，最大亏损最好控制在10%。",
            "我会按10%的最大亏损约束考虑。",
        ),
        (
            "我最近关注新能源车产业链。",
            "新能源车产业链可以拆成整车、电池、材料和充电环节。",
        ),
    ]

    migration_job_ids = []
    for user_message, assistant_message in turns:
        add_result = memory.add(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ],
            user_id="u1",
        )
        if add_result["background"]["migration_job_id"]:
            migration_job_ids.append(add_result["background"]["migration_job_id"])

    assert migration_job_ids
    assert memory.flush_background_tasks(5)
    pages = memory.midterm_memory.list_pages(filters=filters, top_k=10)
    sessions = memory.midterm_memory.list_sessions(filters=filters, top_k=10)
    assert pages
    assert sessions
    assert {row.payload["source_job_id"] for row in pages}.issubset(set(migration_job_ids))
    assert all(set(row.payload.get("source_job_ids") or []) <= set(migration_job_ids) for row in sessions)
    longterm_rows = memory.vector_store.list(filters=filters, top_k=10)
    longterm_job_ids = {
        job["job_id"] for job in memory.db.list_longterm_extraction_jobs(session_scope="user_id=u1")
    }
    assert longterm_job_ids
    assert {row.payload["source_job_id"] for row in longterm_rows}.issubset(longterm_job_ids)
    assert all(row.payload.get("source_job_type") == "longterm_extraction" for row in longterm_rows)
    for row in [*pages, *sessions]:
        assert row.payload["created_at"].endswith("+08:00")
        assert row.payload["updated_at"].endswith("+08:00")

    search_result = memory.search("我能接受多大亏损？", filters=filters, top_k=5)
    sources = {item.get("source") for item in search_result["results"]}
    assert {"long_term", "mid_term_session", "mid_term_page"}.issubset(sources)
    memory.close()


def test_memory_expands_split_eviction_to_complete_qa(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="midterm_split_eviction")
    config.midterm.short_term_capacity = 4
    memory = Memory(config)
    messages = [
        {"role": "user", "content": "我比较保守，最大亏损最好控制在10%。"},
        {"role": "assistant", "content": "我会按10%的最大亏损约束考虑。"},
        {"role": "user", "content": "我偏中长期投资，不喜欢短线。"},
        {"role": "assistant", "content": "后续建议会偏中长期。"},
        {"role": "user", "content": "我最近关注新能源车产业链。"},
    ]

    for message in messages:
        memory.add([message], user_id="u1", infer=False)

    assert memory.flush_background_tasks(5)
    pages = memory.midterm_memory.list_pages(filters={"user_id": "u1"}, top_k=10)
    assert len(pages) == 1
    page = pages[0].payload
    assert page["user_input"] == "我比较保守，最大亏损最好控制在10%。"
    assert page["assistant_response"] == "我会按10%的最大亏损约束考虑。"
    retained_messages = memory.db.get_last_messages("user_id=u1", limit=10)
    assert [message["content"] for message in retained_messages] == [
        "我偏中长期投资，不喜欢短线。",
        "后续建议会偏中长期。",
        "我最近关注新能源车产业链。",
    ]
    memory.close()


def test_memory_reset_clears_midterm_and_lazy_state(tmp_path, fake_memory_env):
    memory = Memory(_memory_config(tmp_path, collection_name="midterm_reset"))
    memory.add(
        [
            {"role": "user", "content": "我比较保守，最大亏损最好控制在10%。"},
            {"role": "assistant", "content": "我会记住这个风险约束。"},
        ],
        user_id="u1",
        infer=False,
    )
    memory.add(
        [
            {"role": "user", "content": "我偏中长期投资，不喜欢短线。"},
            {"role": "assistant", "content": "我会减少短线假设。"},
        ],
        user_id="u1",
        infer=False,
    )

    assert memory.flush_background_tasks(5)
    assert memory.midterm_memory.list_pages(filters={"user_id": "u1"}, top_k=10)
    memory.reset()
    assert memory._midterm_memory is None
    assert memory._midterm_updater is None
    assert memory._midterm_retriever is None
    legacy_eviction_attribute = "_last_" + "evicted_messages"
    assert not hasattr(memory, legacy_eviction_attribute)
    assert memory.midterm_memory.list_pages(filters={"user_id": "u1"}, top_k=10) == []
    assert memory.midterm_memory.list_sessions(filters={"user_id": "u1"}, top_k=10) == []
    memory.close()


def test_midterm_disabled_preserves_search_shape_and_lazy_state(tmp_path, fake_memory_env):
    memory = Memory(_memory_config(tmp_path, enabled=False, collection_name="midterm_disabled"))
    memory.add(
        [
            {"role": "user", "content": "用户偏好保守投资。"},
            {"role": "assistant", "content": "我会记住。"},
        ],
        user_id="u1",
        infer=False,
    )
    memory.add(
        [
            {"role": "user", "content": "用户关注流动性。"},
            {"role": "assistant", "content": "我会纳入考虑。"},
        ],
        user_id="u1",
        infer=False,
    )

    assert memory.flush_background_tasks(5)
    result = memory.search("保守投资", filters={"user_id": "u1"}, top_k=3)
    assert result["results"]
    assert "source" not in result["results"][0]
    assert memory._midterm_memory is None
    assert memory._midterm_updater is None
    assert memory._midterm_retriever is None
    assert not any(name.endswith("_midterm_pages") or name.endswith("_midterm_sessions") for name in fake_memory_env)
    memory.close()


def _insert_midterm_page_and_session(
    memory,
    *,
    page_id="page-1",
    session_id="session-1",
    run_id="run-1",
    page_sequence=1,
):
    now = beijing_now().isoformat()
    scope = f"run_id={run_id}&user_id=user-1"
    if memory.db.current_turn_index(scope) == 0:
        memory.db.save_messages(
            [
                {"role": "user", "content": "seed turn"},
                {"role": "assistant", "content": "seed response"},
            ],
            scope,
            max_messages=100,
        )
    memory.midterm_memory.insert_page(
        page_id,
        {
            "id": page_id,
            "session_id": session_id,
            "raw_dialogue": "User: maximum loss?\n\nAssistant: 10%",
            "user_input": "maximum loss?",
            "assistant_response": "10%",
            "summary": "The maximum acceptable loss is 10%.",
            "keywords": ["loss", "10%"],
            "created_at": now,
            "updated_at": now,
            "user_id": "user-1",
            "run_id": run_id,
            "page_sequence": page_sequence,
            "turn_index": page_sequence,
            "valid_recall_count": 0,
            "last_recall_at": None,
            "last_recall_turn_index": None,
            "output_state": "committed",
        },
    )
    memory.midterm_memory.insert_session(
        session_id,
        {
            "id": session_id,
            "summary": "The user discussed a 10% loss limit.",
            "summary_keywords": ["loss", "10%"],
            "page_ids": [page_id],
            "N_visit": 0,
            "valid_recall_count": 0,
            "last_recall_at": None,
            "L_interaction": 1,
            "R_recency": 1.0,
            "H_segment": 0.5,
            "created_turn_index": page_sequence,
            "last_visit_turn_index": page_sequence,
            "created_at": now,
            "updated_at": now,
            "user_id": "user-1",
            "run_id": run_id,
            "output_state": "committed",
        },
    )


def _add_numbered_turn(memory, turn_index, *, run_id="run-1"):
    memory.add(
        [
            {"role": "user", "content": f"risk discussion q{turn_index}"},
            {"role": "assistant", "content": f"risk response a{turn_index}"},
        ],
        user_id="user-1",
        run_id=run_id,
        infer=False,
    )


def _page_for_turn(memory, turn_index):
    return next(
        row
        for row in memory.midterm_memory.list_pages(
            filters={"user_id": "user-1", "run_id": "run-1"},
            top_k=100,
        )
        if row.payload["turn_index"] == turn_index
    )


def test_short_term_turns_advance_midterm_forgetting_without_new_page_clock(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="true_turn_window")
    config.background.enabled = False
    config.midterm.short_term_capacity = 6
    config.midterm.retention_half_life_turns = 4.0
    config.midterm.retention_floor = 0.0
    config.midterm.heat_alpha = 0.0
    config.midterm.heat_beta = 0.0
    config.midterm.heat_gamma = 0.0
    memory = Memory(config)
    scope = "run_id=run-1&user_id=user-1"
    try:
        for turn_index in range(1, 5):
            _add_numbered_turn(memory, turn_index)

        q1_page = _page_for_turn(memory, 1)
        q1_at_turn_4 = next(
            item
            for item in memory.midterm_retriever.search(
                "risk discussion q1",
                {"user_id": "user-1", "run_id": "run-1"},
            )
            if item.get("id") == str(q1_page.id)
        )
        assert memory.db.current_turn_index(scope) == 4

        _add_numbered_turn(memory, 5)
        q1_at_turn_5 = next(
            item
            for item in memory.midterm_retriever.search(
                "risk discussion q1",
                {"user_id": "user-1", "run_id": "run-1"},
            )
            if item.get("id") == str(q1_page.id)
        )

        current_page = memory.midterm_memory.get_page(str(q1_page.id)).payload
        assert current_page["turn_index"] == 1
        assert memory.db.current_turn_index(scope) == 5
        assert memory.midterm_memory.current_turn_index({"user_id": "user-1", "run_id": "run-1"}) == 5
        assert 5 - current_page["turn_index"] == 4
        assert q1_at_turn_5["forgetting_factor"] < q1_at_turn_4["forgetting_factor"]
    finally:
        memory.close()


def test_recall_and_heat_recency_use_true_conversation_turn(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="true_turn_recall")
    config.background.enabled = False
    config.midterm.short_term_capacity = 6
    config.midterm.heat_recency_tau_turns = 4.0
    memory = Memory(config)
    filters = {"user_id": "user-1", "run_id": "run-1"}
    scope = "run_id=run-1&user_id=user-1"
    try:
        for turn_index in range(1, 6):
            _add_numbered_turn(memory, turn_index)
        q1_page = _page_for_turn(memory, 1)
        page_id = str(q1_page.id)
        session_id = q1_page.payload["session_id"]

        memory._confirm_valid_midterm_page_ids([page_id], current_turn_index=5)
        recalled_page = memory.midterm_memory.get_page(page_id).payload
        recalled_session = memory.midterm_memory.get_session(session_id).payload
        assert recalled_page["last_recall_turn_index"] == 5
        assert recalled_session["N_visit"] == 1
        assert recalled_session["last_visit_turn_index"] == 5

        for turn_index in range(6, 9):
            _add_numbered_turn(memory, turn_index)
        assert memory.db.current_turn_index(scope) == 8
        assert 8 - memory.midterm_memory.get_page(page_id).payload["last_recall_turn_index"] == 3

        _add_numbered_turn(memory, 9)
        session_result = next(
            item
            for item in memory.midterm_retriever.search("risk discussion q1", filters)
            if item.get("id") == session_id
        )
        assert memory.db.current_turn_index(scope) == 9
        assert session_result["R_recency"] == pytest.approx(math.exp(-(9 - 5) / 4.0))
    finally:
        memory.close()


def test_delayed_migration_and_retry_preserve_source_turn_index(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="delayed_turn_migration")
    config.background.enabled = False
    config.midterm.short_term_capacity = 100
    memory = Memory(config)
    filters = {"user_id": "user-1", "run_id": "run-1"}
    scope = "run_id=run-1&user_id=user-1"
    try:
        for turn_index in range(1, 5):
            _add_numbered_turn(memory, turn_index)
        source_q1 = memory.db.get_messages(scope, limit=2)
        assert memory.db.current_turn_index(scope) == 4
        assert {message["turn_index"] for message in source_q1} == {1}

        first = memory.midterm_updater.process_evicted_messages(
            source_q1,
            filters,
            source_job_id="delayed-q1",
            lease_token="lease-1",
        )
        retried = memory.midterm_updater.process_evicted_messages(
            source_q1,
            filters,
            source_job_id="delayed-q1",
            lease_token="lease-2",
        )

        assert first[0]["turn_index"] == 1
        assert retried[0]["turn_index"] == 1
        assert retried[0]["id"] == first[0]["id"]
        assert retried[0]["page_sequence"] == first[0]["page_sequence"]
    finally:
        memory.close()


def test_valid_recall_is_confirmed_only_for_pages_entering_context(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="valid_recall_context")
    config.background.enabled = False
    config.midterm.promotion_min_recall_count = 10
    memory = Memory(config)
    _insert_midterm_page_and_session(memory)
    filters = {"user_id": "user-1", "run_id": "run-1"}

    candidates = memory.midterm_retriever.search("maximum loss", filters)
    assert memory.midterm_memory.get_page("page-1").payload["valid_recall_count"] == 0
    assert memory.midterm_memory.get_session("session-1").payload["valid_recall_count"] == 0

    memory._confirm_context_valid_recalls(candidates, current_turn_index=1)
    page = memory.midterm_memory.get_page("page-1").payload
    session = memory.midterm_memory.get_session("session-1").payload
    assert page["valid_recall_count"] == 1
    assert page["last_recall_at"]
    assert page["last_recall_turn_index"] == 1
    assert "memory_strength" not in page
    assert session["valid_recall_count"] == 1
    assert session["N_visit"] == 1
    assert session["last_visit_turn_index"] == 1
    assert session["R_recency"] == pytest.approx(1.0)
    assert session["H_segment"] > 0.5

    # Deduplication is per confirmation round, even if two retrieval paths
    # produced the same page.
    memory._confirm_context_valid_recalls([*candidates, *candidates], current_turn_index=1)
    assert memory.midterm_memory.get_page("page-1").payload["valid_recall_count"] == 2
    memory.close()


def test_valid_recall_resets_turn_recency_anchor_and_future_search_recomputes_it(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="valid_recall_turn_recency")
    config.background.enabled = False
    config.midterm.heat_recency_tau_turns = 4.0
    memory = Memory(config)
    _insert_midterm_page_and_session(memory)

    memory.midterm_memory.record_valid_recalls(["page-1"], recall_turn_index=4)
    recalled_page = memory.midterm_memory.get_page("page-1").payload
    recalled_session = memory.midterm_memory.get_session("session-1").payload
    assert forgetting_factor(recalled_page, config.midterm, current_turn_index=4) == pytest.approx(1.0)
    assert recalled_session["N_visit"] == 1
    assert recalled_session["last_visit_turn_index"] == 4
    assert recalled_session["R_recency"] == pytest.approx(1.0)

    memory.db.save_messages(
        [
            {"role": "user", "content": "turn 5"},
            {"role": "assistant", "content": "turn 5 response"},
            {"role": "user", "content": "turn 6"},
            {"role": "assistant", "content": "turn 6 response"},
            {"role": "user", "content": "turn 7"},
            {"role": "assistant", "content": "turn 7 response"},
            {"role": "user", "content": "turn 8"},
            {"role": "assistant", "content": "turn 8 response"},
            {"role": "user", "content": "turn 9"},
            {"role": "assistant", "content": "turn 9 response"},
            {"role": "user", "content": "turn 10"},
            {"role": "assistant", "content": "turn 10 response"},
            {"role": "user", "content": "turn 11"},
            {"role": "assistant", "content": "turn 11 response"},
        ],
        "run_id=run-1&user_id=user-1",
        max_messages=100,
    )
    memory.midterm_memory.insert_page(
        "page-clock",
        {
            "id": "page-clock",
            "session_id": "session-clock",
            "summary": "conversation advanced",
            "raw_dialogue": "User: next\n\nAssistant: next",
            "created_at": beijing_now().isoformat(),
            "updated_at": beijing_now().isoformat(),
            "user_id": "user-1",
            "run_id": "run-1",
            "page_sequence": 8,
            "turn_index": 2,
            "output_state": "committed",
        },
    )
    results = memory.midterm_retriever.search(
        "maximum loss",
        {"user_id": "user-1", "run_id": "run-1"},
    )
    recalled_result = next(item for item in results if item.get("id") == "session-1")
    assert recalled_result["R_recency"] == pytest.approx(math.exp(-1.0))
    memory.close()


def test_threshold_filtered_midterm_page_is_not_reinforced(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="threshold_no_recall")
    config.background.enabled = False
    config.midterm.midterm_rag_threshold = 1.0
    memory = Memory(config)
    _insert_midterm_page_and_session(memory)

    results = memory.midterm_retriever.search(
        "unrelated bonds",
        {"user_id": "user-1", "run_id": "run-1"},
    )
    memory._confirm_context_valid_recalls(results, current_turn_index=1)

    assert _page_results(results) == []
    assert memory.midterm_memory.get_page("page-1").payload["valid_recall_count"] == 0
    assert memory.midterm_memory.get_session("session-1").payload["valid_recall_count"] == 0
    memory.close()


def test_existing_longterm_configured_rag_threshold_applies_cross_session_weight(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, enabled=False, collection_name="longterm_threshold")
    config.background.enabled = False
    config.longterm_rag_threshold = 0.8
    memory = Memory(config)
    rows = [
        ("relevant", "The maximum acceptable loss is 10%.", "run-1"),
        ("irrelevant", "The user prefers bond funds.", "run-1"),
        ("other-run", "The maximum acceptable loss is 10%.", "run-2"),
    ]
    memory.vector_store.insert(
        ids=[row[0] for row in rows],
        vectors=[memory.embedding_model.embed(row[1], "add") for row in rows],
        payloads=[
            {
                "data": text,
                "user_id": "user-1",
                "run_id": run_id,
                "created_at": beijing_now().isoformat(),
                "updated_at": beijing_now().isoformat(),
            }
            for _, text, run_id in rows
        ],
    )

    results = memory.search(
        "maximum acceptable loss 10%",
        filters={"user_id": "user-1", "run_id": "run-1"},
        top_k=5,
        threshold=0.0,
    )["results"]

    assert [item["id"] for item in results] == ["relevant", "other-run"]
    assert results[0]["run_id"] == "run-1"
    assert results[1]["run_id"] == "run-2"
    assert results[0]["score"] > results[1]["score"]
    memory.close()


def test_cross_session_longterm_promotion_is_user_scoped_and_idempotent(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="cross_session_promotion")
    config.background.poll_interval_seconds = 0.01
    config.background.retry_delays_seconds = (0.0,)
    config.midterm.promotion_min_recall_count = 2
    config.midterm.promotion_heat_threshold = 0.0
    memory = Memory(config)
    _insert_midterm_page_and_session(memory, run_id="run-a")
    recalled_page = {
        "id": "page-1",
        "source": "mid_term_page",
        "raw_dialogue": "User: maximum loss?\n\nAssistant: 10%",
    }

    memory._confirm_context_valid_recalls([recalled_page], current_turn_index=1)
    assert memory.cross_session_longterm.list(filters={"user_id": "user-1"}, top_k=10) == []
    memory._confirm_context_valid_recalls([recalled_page], current_turn_index=1)
    assert memory.flush_background_tasks(5)

    promoted = memory.cross_session_longterm.list(filters={"user_id": "user-1"}, top_k=10)
    assert len(promoted) == 1
    payload = promoted[0].payload
    assert payload["source"] == "cross_session_long_term"
    assert payload["source_midterm_session_id"] == "session-1"
    assert payload["source_run_id"] == "run-a"
    assert payload["source_page_ids"] == ["page-1"]
    assert payload["evidence"][0]["raw_dialogue"]
    assert "run_id" not in payload

    new_session_results = memory._with_midterm_search_results(
        "maximum loss 10%",
        {"user_id": "user-1", "run_id": "run-b"},
        [],
    )
    cross_session_results = [item for item in new_session_results if item.get("source") == "cross_session_long_term"]
    assert len(cross_session_results) == 1
    assert cross_session_results[0]["source_run_id"] == "run-a"
    assert not [item for item in new_session_results if item.get("source") == "long_term"]
    assert memory.cross_session_longterm.get(promoted[0].id).payload["recall_count"] == 0

    memory._confirm_context_valid_recalls(cross_session_results, current_turn_index=1)
    recalled = memory.cross_session_longterm.get(promoted[0].id).payload
    assert recalled["recall_count"] == 1
    assert recalled["last_recall_at"]
    assert recalled["memory_strength"] > 1.0

    original_memory_id = promoted[0].id
    original_source_version = recalled["source_version"]
    session = memory.midterm_memory.get_session("session-1").payload
    session["summary"] = "The user discussed a 10% loss limit and long-term investing."
    session["summary_keywords"] = ["loss", "10%", "long-term"]
    memory.midterm_memory.update_session("session-1", session, reembed=False)
    memory._confirm_context_valid_recalls([recalled_page], current_turn_index=1)
    assert memory.flush_background_tasks(5)

    refreshed_rows = memory.cross_session_longterm.list(filters={"user_id": "user-1"}, top_k=10)
    refreshed = memory.cross_session_longterm.get(original_memory_id).payload
    assert len(refreshed_rows) == 1
    assert refreshed_rows[0].id == original_memory_id
    assert refreshed["source_version"] != original_source_version
    assert len(memory.db.list_promotion_jobs(source_midterm_session_id="session-1")) == 2
    memory.close()


def test_cross_session_retrieval_uses_slow_reinforcement_and_raw_threshold(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="cross_session_evolution")
    config.background.enabled = False
    config.midterm.promotion_min_recall_count = 1
    config.midterm.promotion_heat_threshold = 0.0
    config.cross_session_longterm_rag_threshold = 0.8
    memory = Memory(config)
    _insert_midterm_page_and_session(memory, run_id="run-a")
    memory.midterm_memory.record_valid_recalls(["page-1"], recall_turn_index=1)
    promoted = memory.cross_session_longterm.promote_session("session-1", memory.midterm_memory)
    assert promoted["memory_strength"] == pytest.approx(1.0)

    memory_id = promoted["id"]
    row = memory.cross_session_longterm.get(memory_id)
    payload = dict(row.payload)
    now = beijing_now()
    old = (now - timedelta(hours=config.cross_session_retention_half_life_hours)).isoformat()
    payload["promoted_at"] = old
    payload["last_recall_at"] = None
    memory.cross_session_longterm.store.update(vector_id=memory_id, vector=None, payload=payload)

    filtered = memory.cross_session_longterm.search(
        "bond fund allocation",
        user_id="user-1",
        top_k=5,
        threshold=config.cross_session_longterm_rag_threshold,
        now=now.isoformat(),
    )
    assert filtered == []
    assert memory.cross_session_longterm.get(memory_id).payload["recall_count"] == 0

    candidates = memory.cross_session_longterm.search(
        "maximum loss 10%",
        user_id="user-1",
        top_k=5,
        threshold=config.cross_session_longterm_rag_threshold,
        now=now.isoformat(),
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["forgetting_factor"] == pytest.approx(0.5)
    assert candidate["final_score"] == pytest.approx(candidate["raw_rag_score"] * candidate["forgetting_factor"])
    assert candidate["final_score"] < config.cross_session_longterm_rag_threshold
    assert memory.cross_session_longterm.get(memory_id).payload["recall_count"] == 0

    memory._confirm_context_valid_recalls(candidates, current_turn_index=1)
    recalled = memory.cross_session_longterm.get(memory_id).payload
    assert recalled["recall_count"] == 1
    assert recalled["last_recall_at"]
    assert recalled["memory_strength"] == pytest.approx(memory_strength(1, config.cross_session_reinforcement_gain))

    cross_payload = {"promoted_at": old, "recall_count": 0}
    cross_retention = forgetting_factor(
        cross_payload,
        config.midterm,
        now=now.isoformat(),
        recall_count_key="recall_count",
        anchor_keys=("last_recall_at", "promoted_at"),
        half_life_hours=config.cross_session_retention_half_life_hours,
        retention_floor=config.cross_session_retention_floor,
        reinforcement_gain=config.cross_session_reinforcement_gain,
    )
    midterm_retention = forgetting_factor(
        {"created_at": old, "turn_index": 0, "valid_recall_count": 99},
        config.midterm,
        now=now.isoformat(),
        current_turn_index=config.midterm.retention_half_life_turns,
    )
    assert cross_retention == pytest.approx(0.5)
    assert midterm_retention == pytest.approx(0.5)
    assert forgetting_factor(
        {"created_at": now.isoformat(), "turn_index": 0, "valid_recall_count": 0},
        config.midterm,
        now=(now + timedelta(days=365)).isoformat(),
        current_turn_index=config.midterm.retention_half_life_turns,
    ) == pytest.approx(midterm_retention)
    memory.close()


def test_valid_recall_only_enqueues_promotion_without_embedding(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="promotion_enqueue_only")
    config.background.enabled = False
    config.midterm.promotion_min_recall_count = 1
    config.midterm.promotion_heat_threshold = 0.0
    memory = Memory(config)
    _insert_midterm_page_and_session(memory)
    original_embed = memory.embedding_model.embed
    embedding_calls = []

    def recording_embed(text, memory_action=None):
        embedding_calls.append(memory_action)
        return original_embed(text, memory_action)

    memory.embedding_model.embed = recording_embed
    embedding_calls.clear()
    memory._confirm_context_valid_recalls(
        [{"id": "page-1", "source": "mid_term_page", "raw_dialogue": "context"}],
        current_turn_index=1,
    )

    jobs = memory.db.list_promotion_jobs()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "pending"
    assert embedding_calls == []
    assert memory.cross_session_longterm.list(filters={"user_id": "user-1"}, top_k=10) == []
    memory.close()


def test_blocked_promotion_does_not_block_another_memory_valid_recall(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="promotion_recall_lock_isolation")
    config.background.enabled = False
    config.midterm.promotion_min_recall_count = 1
    config.midterm.promotion_heat_threshold = 0.0
    memory = Memory(config)
    _insert_midterm_page_and_session(memory, page_id="page-a", session_id="session-a")
    _insert_midterm_page_and_session(memory, page_id="page-b", session_id="session-b")
    memory.midterm_memory.record_valid_recalls(["page-a", "page-b"], recall_turn_index=1)
    session_a = memory.midterm_memory.get_session("session-a").payload
    session_a["summary"] = "blocked-promotion-session-a"
    memory.midterm_memory.update_session("session-a", session_a, reembed=False)
    promoted_b = memory.cross_session_longterm.promote_session("session-b", memory.midterm_memory)

    original_embed = memory.embedding_model.embed
    promotion_entered = threading.Event()
    release_promotion = threading.Event()

    def blocking_embed(text, memory_action=None):
        if "blocked-promotion-session-a" in text and memory_action in {"add", "update"}:
            promotion_entered.set()
            assert release_promotion.wait(3)
        return original_embed(text, memory_action)

    memory.embedding_model.embed = blocking_embed
    promotion_thread = threading.Thread(
        target=memory.cross_session_longterm.promote_session,
        args=("session-a", memory.midterm_memory),
    )
    recall_finished = threading.Event()
    recall_thread = threading.Thread(
        target=lambda: (
            memory.cross_session_longterm.record_valid_recalls([promoted_b["id"]]),
            recall_finished.set(),
        )
    )
    try:
        promotion_thread.start()
        assert promotion_entered.wait(1)
        recall_thread.start()
        assert recall_finished.wait(1), "another memory's recall was blocked by promotion embedding"
    finally:
        release_promotion.set()
        promotion_thread.join(3)
        recall_thread.join(3)

    assert not promotion_thread.is_alive()
    assert not recall_thread.is_alive()
    assert memory.cross_session_longterm.get(promoted_b["id"]).payload["recall_count"] == 1
    memory.close()


def test_different_sessions_enter_promotion_embedding_concurrently(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="promotion_true_concurrency")
    config.background.enabled = False
    config.midterm.promotion_min_recall_count = 1
    config.midterm.promotion_heat_threshold = 0.0
    memory = Memory(config)
    _insert_midterm_page_and_session(memory, page_id="page-a", session_id="session-a")
    _insert_midterm_page_and_session(memory, page_id="page-b", session_id="session-b")
    memory.midterm_memory.record_valid_recalls(["page-a", "page-b"], recall_turn_index=1)
    for session_id in ("session-a", "session-b"):
        session = memory.midterm_memory.get_session(session_id).payload
        session["summary"] = f"slow-promotion-{session_id}"
        memory.midterm_memory.update_session(session_id, session, reembed=False)

    original_embed = memory.embedding_model.embed
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    both_entered = threading.Event()
    release_promotions = threading.Event()

    def concurrent_embed(text, memory_action=None):
        nonlocal active, max_active
        if "slow-promotion-session-" not in text or memory_action not in {"add", "update"}:
            return original_embed(text, memory_action)
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_entered.set()
        try:
            assert release_promotions.wait(3)
            return original_embed(text, memory_action)
        finally:
            with state_lock:
                active -= 1

    memory.embedding_model.embed = concurrent_embed
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(memory.cross_session_longterm.promote_session, session_id, memory.midterm_memory)
            for session_id in ("session-a", "session-b")
        ]
        try:
            assert both_entered.wait(1), "different-session promotions were serialized"
        finally:
            release_promotions.set()
        promoted = [future.result(timeout=3) for future in futures]

    assert max_active == 2
    assert all(item is not None for item in promoted)
    assert len(memory.cross_session_longterm.list(filters={"user_id": "user-1"}, top_k=10)) == 2
    memory.close()

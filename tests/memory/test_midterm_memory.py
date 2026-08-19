import asyncio
import json
import math
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from mem0 import Memory
from mem0.configs.base import MemoryConfig, MidTermMemoryConfig
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT
from mem0.memory.midterm_retriever import MidTermRetriever
from mem0.memory.midterm_updater import MidTermUpdater
from mem0.memory.storage import SQLiteManager


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
        if "将一轮已从短期记忆淘汰" in system:
            return json.dumps(self._page_summary(user_prompt), ensure_ascii=False)
        if "将现有的中期记忆会话摘要" in system:
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
    assert "只返回一个 JSON 对象" in MIDTERM_PAGE_SUMMARY_PROMPT
    assert "用户的意图、偏好、约束和讨论主题" in MIDTERM_PAGE_SUMMARY_PROMPT
    assert "summary" in MIDTERM_PAGE_SUMMARY_PROMPT
    assert "keywords" in MIDTERM_PAGE_SUMMARY_PROMPT
    assert "只返回一个 JSON 对象" in MIDTERM_SESSION_MERGE_PROMPT
    assert "整个会话" in MIDTERM_SESSION_MERGE_PROMPT
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
        {"role": "user", "content": '用户第一行\n用户第二行，含引号 " 和 emoji 😀'},
        {"role": "assistant", "content": "助手第一行\n助手第二行，含反斜杠 \\"},
    ]
    expected = (
        'User: 用户第一行\n用户第二行，含引号 " 和 emoji 😀\n\n'
        "Assistant: 助手第一行\n助手第二行，含反斜杠 \\"
    )

    try:
        pages = memory.midterm_updater.process_evicted_messages(
            messages,
            {"user_id": "u1", "run_id": "r1"},
        )
        summary_call = next(
            call
            for call in llm.calls
            if call["messages"][0]["content"] == MIDTERM_PAGE_SUMMARY_PROMPT
        )

        assert summary_call["messages"][1]["content"] == expected
        assert pages[0]["raw_dialogue"] == expected
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


def test_midterm_config_default_disabled():
    config = MemoryConfig()
    assert config.midterm.enabled is False
    assert config.midterm.short_term_capacity == 10
    assert config.midterm.max_total_pages == 4


def test_midterm_config_rejects_negative_page_limits():
    with pytest.raises(ValueError):
        MidTermMemoryConfig(top_k_sessions=-1)
    with pytest.raises(ValueError):
        MidTermMemoryConfig(top_k_pages=-1)
    with pytest.raises(ValueError):
        MidTermMemoryConfig(max_total_pages=-1)


def _midterm_row(row_id, score, **payload):
    return SimpleNamespace(id=row_id, score=score, payload=payload)


class FakeMidTermRetrievalStore:
    def __init__(self, sessions, pages_by_session):
        self.sessions = sessions
        self.pages_by_session = pages_by_session
        self.session_filters = []
        self.page_filters = []
        self.visited_sessions = []

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


def test_midterm_retriever_max_total_pages_zero_returns_no_pages():
    sessions = [_midterm_row("s1", 0.9, summary="session 1", user_id="u1", run_id="r1")]
    pages_by_session = {"s1": [_midterm_row("p1", 0.7, session_id="s1", summary="page 1", user_id="u1", run_id="r1")]}
    store = FakeMidTermRetrievalStore(sessions, pages_by_session)
    retriever = MidTermRetriever(store, _retriever_config(max_total_pages=0))

    results = retriever.search("risk", {"user_id": "u1", "run_id": "r1"})

    assert _page_results(results) == []
    assert [item["source"] for item in results] == ["mid_term_session"]
    assert store.page_filters == []


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
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
    )
    assert pairs == [
        {"user_input": "u1", "assistant_response": "a1", "created_at": None},
        {"user_input": "u2", "assistant_response": "", "created_at": None},
    ]
    assert MidTermUpdater._messages_to_qa_pairs([{"role": "assistant", "content": "assistant-only"}]) == []


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
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second question"},
                {"role": "assistant", "content": "second answer"},
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
    await asyncio.gather(
        *(updater._summarize_page_async(f"user-{index}", f"assistant-{index}") for index in range(8))
    )
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
        {"role": "user", "content": "我偏好中长期投资。"},
        {"role": "assistant", "content": "我会记住。"},
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


def test_discarded_midterm_does_not_pollute_existing_session_summary(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="midterm_discard")
    config.background.enabled = False
    memory = Memory(config)
    filters = {"user_id": "u1", "run_id": "r1"}
    memory._process_midterm_evictions(
        [
            {"role": "user", "content": "我比较保守，最大亏损10%。"},
            {"role": "assistant", "content": "我会按这个约束考虑。"},
        ],
        filters,
        raise_on_error=True,
    )
    before = {
        row.id: dict(row.payload)
        for row in memory.midterm_memory.list_sessions(filters=filters, top_k=10)
    }

    memory._process_midterm_evictions(
        [
            {"role": "user", "content": "这条内容不应进入现有摘要。"},
            {"role": "assistant", "content": "临时回答。"},
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
    after = {
        row.id: dict(row.payload)
        for row in memory.midterm_memory.list_sessions(filters=filters, top_k=10)
    }
    staged = memory.midterm_memory.list_pages(
        filters=filters,
        top_k=10,
        include_uncommitted=True,
    )

    assert cleanup_error is None
    assert before == after
    assert any(row.payload.get("output_state") == "discarded" for row in staged)
    assert all(row.payload.get("source_job_id") != "discarded-midterm-job" for row in memory.midterm_memory.list_pages(filters=filters))
    memory.close()


def test_old_midterm_lease_cannot_cleanup_new_lease_output(tmp_path, fake_memory_env):
    config = _memory_config(tmp_path, collection_name="midterm_cleanup_fencing")
    config.background.enabled = False
    memory = Memory(config)
    filters = {"user_id": "u1", "run_id": "r1"}
    memory._process_midterm_evictions(
        [
            {"role": "user", "content": "新的租约应保留这条页面。"},
            {"role": "assistant", "content": "收到。"},
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
            {"role": "user", "content": "旧租约页面。"},
            {"role": "assistant", "content": "旧租约回答。"},
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
            {"role": "user", "content": "已提交页面。"},
            {"role": "assistant", "content": "不会被旧租约清理。"},
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
        {"role": "user", "content": "我偏好中长期投资。"},
        {"role": "assistant", "content": "我会记住。"},
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
    assert {row.payload["source_job_id"] for row in longterm_rows}.issubset(set(migration_job_ids))
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

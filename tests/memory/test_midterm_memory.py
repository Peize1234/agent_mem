import json
import math
from types import SimpleNamespace

import pytest

from mem0 import Memory
from mem0.configs.base import MemoryConfig, MidTermMemoryConfig
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
        if "Summarize one evicted" in system:
            return json.dumps(self._page_summary(user_prompt), ensure_ascii=False)
        if "Merge an existing mid-term session summary" in system:
            payload = json.loads(user_prompt)
            existing = payload.get("existing_session", {})
            new_page = payload.get("new_page", {})
            summary = " ".join(
                part for part in [existing.get("summary", ""), new_page.get("summary", "")] if part
            )
            keywords = []
            for keyword in existing.get("keywords", []) + new_page.get("keywords", []):
                if keyword not in keywords:
                    keywords.append(keyword)
            return json.dumps({"summary": summary[:1000], "keywords": keywords[:12]}, ensure_ascii=False)

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

    def create_vector_store(provider, config):
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
                    "keywords": ["风险", "风险", "亏损", "10%", "中长期", "短线", "新能源", "基金", "债券", "配置", "行业", "产业链", "超额"],
                },
                ensure_ascii=False,
            )

    updater = MidTermUpdater(midterm_memory=None, llm=MergeLLM(), config=MidTermMemoryConfig())
    summary, keywords = updater._merge_session("old", ["风险"], "new", ["亏损"])
    assert len(summary) <= 1000
    assert summary.startswith("merged")
    assert keywords == ["风险", "亏损", "10%", "中长期", "短线", "新能源", "基金", "债券", "配置", "行业", "产业链", "超额"]


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

    assert memory.midterm_memory.list_pages(filters=filters, top_k=10)
    result = memory.search("我能接受多大亏损？", filters=filters, top_k=5)
    sources = {item.get("source") for item in result["results"]}
    assert {"long_term", "mid_term_session", "mid_term_page"}.issubset(sources)


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

    add_results = []
    for user_message, assistant_message in turns:
        add_result = memory.add(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ],
            user_id="u1",
        )
        add_results.extend(add_result["results"])

    assert add_results
    assert memory.midterm_memory.list_pages(filters=filters, top_k=10)
    assert memory.midterm_memory.list_sessions(filters=filters, top_k=10)

    search_result = memory.search("我能接受多大亏损？", filters=filters, top_k=5)
    sources = {item.get("source") for item in search_result["results"]}
    assert {"long_term", "mid_term_session", "mid_term_page"}.issubset(sources)


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

    assert memory.midterm_memory.list_pages(filters={"user_id": "u1"}, top_k=10)
    memory.reset()
    assert memory._midterm_memory is None
    assert memory._midterm_updater is None
    assert memory._midterm_retriever is None
    assert memory._last_evicted_messages == []
    assert memory.midterm_memory.list_pages(filters={"user_id": "u1"}, top_k=10) == []
    assert memory.midterm_memory.list_sessions(filters={"user_id": "u1"}, top_k=10) == []


def test_midterm_disabled_preserves_search_shape_and_lazy_state(tmp_path, fake_memory_env):
    memory = Memory(_memory_config(tmp_path, enabled=False, collection_name="midterm_disabled"))
    memory.add("用户偏好保守投资。", user_id="u1", infer=False)

    result = memory.search("保守投资", filters={"user_id": "u1"}, top_k=3)
    assert result["results"]
    assert "source" not in result["results"][0]
    assert memory._midterm_memory is None
    assert memory._midterm_updater is None
    assert memory._midterm_retriever is None
    assert not any(name.endswith("_midterm_pages") or name.endswith("_midterm_sessions") for name in fake_memory_env)

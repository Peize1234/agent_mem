import asyncio
import concurrent.futures
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from mem0.configs.base import FineGrainedLongTermConfig, MemoryConfig, MidTermMemoryConfig
from mem0.embeddings.encoding_contract import EncodingContractEmbedding
from mem0.memory.cross_session_longterm import CrossSessionLongTermMemory
from mem0.memory.fine_grained_longterm import FineGrainedLongTermRetriever
from mem0.memory.main import Memory
from mem0.memory.midterm import MidTermMemory, page_embedding_text
from mem0.memory.midterm_retriever import MidTermRetriever
from mem0.memory.midterm_updater import MidTermUpdater
from mem0.memory.promoted_longterm import PromotedLongTermMemory
from mem0.reranker.concurrency import RerankerConcurrencyGuard


class Point(SimpleNamespace):
    pass


def test_midterm_page_representation_default_and_alternatives():
    payload = {
        "summary": "summary",
        "keywords": ["cash", "flow"],
        "user_input": "user question",
        "assistant_response": "assistant answer",
        "raw_dialogue": "raw dialogue",
    }

    assert page_embedding_text(payload) == "summary\nKeywords: cash, flow\nUser: user question"
    assert page_embedding_text(payload, "P0") == page_embedding_text(payload)
    assert page_embedding_text(payload, "summary") == "summary"
    assert page_embedding_text(payload, "user_assistant") == ("User: user question\nAssistant: assistant answer")
    assert page_embedding_text(payload, "summary_keywords") == "summary\nKeywords: cash, flow"


def test_midterm_dense_and_bm25_fusion_are_production_paths():
    dense = [
        Point(id="dense", score=0.9, payload={"summary": "dense"}),
        Point(id="hybrid", score=0.2, payload={"summary": "hybrid"}),
    ]
    sparse = [
        Point(id="hybrid", score=10.0, payload={"summary": "hybrid"}),
        Point(id="sparse", score=5.0, payload={"summary": "sparse"}),
    ]

    class Store:
        def search(self, **kwargs):
            return dense

        def keyword_search(self, **kwargs):
            return sparse

    memory = MidTermMemory.__new__(MidTermMemory)
    memory.embedding_model = SimpleNamespace(embed=lambda text, action: [1.0])
    memory.pages_store = Store()
    memory.output_is_visible = lambda payload: True
    memory.config = MidTermMemoryConfig(retrieval_method="dense")
    assert [row.id for row in memory.search_pages("query", top_k=2)] == ["dense", "hybrid"]

    memory.config = MidTermMemoryConfig(
        retrieval_method="dense_bm25_fusion",
        dense_weight=0.25,
    )
    fused = memory.search_pages("query", top_k=3)
    assert [row.id for row in fused] == ["hybrid", "dense", "sparse"]
    assert fused[0].score == pytest.approx(0.75)

    memory.config = MidTermMemoryConfig(
        retrieval_method="dense_bm25_fusion",
        fusion_method="rrf",
    )
    assert memory.search_pages("query", top_k=1)[0].id == "hybrid"


def test_midterm_multivector_storage_contract_is_written_by_production():
    inserted = {}

    class Store:
        def insert(self, **kwargs):
            inserted.update(kwargs)

    memory = MidTermMemory.__new__(MidTermMemory)
    memory.config = MidTermMemoryConfig(reranker={"method": "multi_vector_maxsim"})
    memory.embedding_model = SimpleNamespace(embed=lambda text, action: [float(len(text))])
    memory.pages_store = Store()
    memory.insert_page(
        "page-1",
        {"summary": "summary", "keywords": ["key"], "user_input": "question"},
    )

    payload = inserted["payloads"][0]
    assert set(payload["_field_vectors"]) == {"summary", "keywords", "user_input"}
    assert inserted["vectors"][0] == [float(len("summary\nKeywords: key\nUser: question"))]


def test_midterm_reranker_uses_deep_pool_and_none_preserves_order():
    rows = [
        {"id": "a", "memory": "A", "score": 0.9, "final_score": 0.9},
        {"id": "b", "memory": "B", "score": 0.8, "final_score": 0.8},
        {"id": "c", "memory": "C", "score": 0.7, "final_score": 0.7},
    ]
    none = MidTermRetriever(SimpleNamespace(), MidTermMemoryConfig())
    assert none._apply_reranker("query", rows) == rows

    class ReverseReranker:
        def rerank(self, query, documents, top_k):
            return [{**row, "rerank_score": score} for row, score in zip(reversed(documents), (3, 2, 1))]

    config = MidTermMemoryConfig(reranker={"method": "cross_encoder", "rerank_depth": 3})
    reranked = MidTermRetriever(SimpleNamespace(), config, reranker=ReverseReranker())._apply_reranker("query", rows)
    assert [row["id"] for row in reranked] == ["c", "b", "a"]
    assert reranked[0]["first_stage_score"] == pytest.approx(0.7)


class FineStore:
    def __init__(self):
        self.depths = []
        self.rows = [
            Point(id="current", score=0.9, payload={"data": "current", "run_id": "r1"}),
            Point(id="other", score=0.85, payload={"data": "other", "run_id": "r2"}),
            Point(id="expired", score=0.8, payload={"data": "expired", "run_id": "r2", "expired": True}),
            Point(id="pending", score=0.95, payload={"data": "pending", "run_id": "r1", "pending": True}),
        ]

    def search(self, *, top_k, filters, **kwargs):
        self.depths.append(top_k)
        if filters.get("run_id"):
            return [row for row in self.rows if row.payload.get("run_id") == filters["run_id"]]
        return list(self.rows)

    def keyword_search(self, **kwargs):
        return [Point(id="other", score=10.0, payload={})]


def _fine_retriever(config=None, reranker=None):
    return FineGrainedLongTermRetriever(
        vector_store=FineStore(),
        embedding_model=SimpleNamespace(embed=lambda *args: [1.0], embed_batch=lambda *args: []),
        entity_store_provider=lambda: None,
        config=config or FineGrainedLongTermConfig(),
        reranker=reranker,
        stage_output_is_visible=lambda payload: not payload.get("pending"),
        payload_is_expired=lambda payload: bool(payload.get("expired")),
        entity_extractor=lambda query: [],
        bm25_language="en",
    )


@pytest.mark.asyncio
async def test_fine_grained_sync_async_cross_session_visibility_and_weight_parity():
    config = FineGrainedLongTermConfig(other_session_weight=0.5)
    sync_retriever = _fine_retriever(config)
    async_retriever = _fine_retriever(config)

    sync_rows = sync_retriever.search("cash flow", {"user_id": "u1", "run_id": "r1"}, top_k=5, explain=True)
    async_rows = await async_retriever.search_async(
        "cash flow", {"user_id": "u1", "run_id": "r1"}, top_k=5, explain=True
    )

    assert [row["id"] for row in sync_rows] == [row["id"] for row in async_rows]
    assert {row["id"] for row in sync_rows} == {"current", "other"}
    details = {row["id"]: row["score_details"] for row in sync_rows}
    assert details["current"]["session_weight"] == 1.0
    assert details["other"]["session_weight"] == 0.5
    assert sync_retriever.vector_store.depths[0] == 60


def test_fine_grained_configurable_weights_and_two_stage_rerank_depth():
    candidates = [
        {"id": "a", "score": 0.9, "payload": {"data": "A", "run_id": "r1"}},
        {"id": "b", "score": 0.8, "payload": {"data": "B", "run_id": "r1"}},
        {"id": "c", "score": 0.7, "payload": {"data": "C", "run_id": "r1"}},
    ]

    class PromoteThird:
        def rerank(self, query, documents, top_k):
            scores = {"a": 0.1, "b": 0.2, "c": 1.0}
            return [
                {**row, "rerank_score": scores[row["id"]]}
                for row in sorted(documents, key=lambda item: scores[item["id"]], reverse=True)
            ]

    config = FineGrainedLongTermConfig(
        top_k=2,
        semantic_weight=0.4,
        bm25_weight=0.4,
        entity_weight=0.2,
        reranker={"method": "cross_encoder", "rerank_depth": 3},
    )
    rows = _fine_retriever(config, PromoteThird()).rank_frozen(
        "query",
        candidates,
        bm25_scores={"b": 1.0},
        entity_boosts={"c": 0.5},
        current_run_id="r1",
        top_k=2,
        threshold=0.1,
    )

    assert [row["id"] for row in rows] == ["c", "b"]
    assert rows[0]["rerank_score"] == 1.0
    assert rows[0]["first_stage_score"] < rows[1]["first_stage_score"]


def test_fine_grained_reranker_failure_falls_back_to_first_stage():
    class Broken:
        def rerank(self, query, documents, top_k):
            raise RuntimeError("broken model")

    config = FineGrainedLongTermConfig(
        top_k=2,
        reranker={"method": "cross_encoder", "rerank_depth": 3},
    )
    candidates = [
        {"id": key, "score": score, "payload": {"data": key}} for key, score in (("a", 0.9), ("b", 0.8), ("c", 0.7))
    ]
    rows = _fine_retriever(config, Broken()).rank_frozen("query", candidates, top_k=2)
    assert [row["id"] for row in rows] == ["a", "b"]


@pytest.mark.asyncio
async def test_reranker_concurrency_guard_bounds_sync_and_async_calls():
    class Delegate:
        def __init__(self):
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()

        def rerank(self, query, documents, top_k):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return documents[:top_k]

    delegate = Delegate()
    guard = RerankerConcurrencyGuard(delegate, max_concurrency=1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: guard.rerank("q", [{"memory": "m"}], 1), range(4)))
    await asyncio.gather(*(guard.rerank_async("q", [{"memory": "m"}], 1) for _ in range(4)))
    assert delegate.maximum == 1


def test_promoted_longterm_rename_keeps_persisted_protocol_and_is_decoupled_from_midterm_top_k():
    assert CrossSessionLongTermMemory is PromotedLongTermMemory
    assert PromotedLongTermMemory.SOURCE == "cross_session_long_term"
    config = MemoryConfig(midterm={"max_total_pages": 9})
    assert config.promoted_longterm.top_k == 4
    assert not hasattr(PromotedLongTermMemory, "rerank")


def test_promoted_longterm_enabled_flag_skips_search_without_initializing_store():
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(
        midterm=SimpleNamespace(enabled=True),
        promoted_longterm=SimpleNamespace(enabled=False),
    )
    memory._midterm_retriever = SimpleNamespace(search=lambda query, filters: [])
    memory._cross_session_longterm = None

    rows = memory._with_midterm_search_results("query", {"user_id": "u1"}, [{"id": "fine"}])

    assert rows == [{"id": "fine", "source": "long_term"}]
    assert memory._cross_session_longterm is None


def test_production_config_validates_weights_and_cross_encoder_dependency():
    with pytest.raises(ValidationError, match="must sum to 1"):
        MemoryConfig(
            fine_grained_longterm={
                "semantic_weight": 0.8,
                "bm25_weight": 0.3,
                "entity_weight": 0.1,
            }
        )
    with pytest.raises(ValidationError, match="requires MemoryConfig.reranker"):
        MemoryConfig(fine_grained_longterm={"reranker": {"method": "cross_encoder"}})
    with pytest.raises(ValidationError, match="cannot override reserved request fields"):
        MemoryConfig(midterm={"page_summary_request_options": {"response_format": {"type": "text"}}})


@pytest.mark.asyncio
async def test_midterm_summary_and_merge_prompts_use_production_config_sync_async():
    class LLM:
        def __init__(self):
            self.system_prompts = []
            self.request_options = []

        def generate_response(self, *, messages, **kwargs):
            self.system_prompts.append(messages[0]["content"])
            self.request_options.append(kwargs.get("extra_body"))
            return '{"summary":"ok","keywords":["key"]}'

        async def generate_response_async(self, *, messages, **kwargs):
            self.system_prompts.append(messages[0]["content"])
            self.request_options.append(kwargs.get("extra_body"))
            return '{"summary":"ok","keywords":["key"]}'

    llm = LLM()
    config = MidTermMemoryConfig(
        page_summary_prompt="custom page prompt",
        session_merge_prompt="custom merge prompt",
        page_summary_request_options={"extra_body": {"thinking": {"type": "disabled"}}},
        session_merge_request_options={"extra_body": {"thinking": {"type": "enabled"}}},
    )
    updater = MidTermUpdater(SimpleNamespace(), llm, config)
    updater._summarize_page("q", "a")
    updater._merge_session("old", [], "new", [])
    await updater._summarize_page_async("q", "a")
    await updater._merge_session_async("old", [], "new", [])
    assert llm.system_prompts == [
        "custom page prompt",
        "custom merge prompt",
        "custom page prompt",
        "custom merge prompt",
    ]
    assert llm.request_options == [
        {"thinking": {"type": "disabled"}},
        {"thinking": {"type": "enabled"}},
        {"thinking": {"type": "disabled"}},
        {"thinking": {"type": "enabled"}},
    ]


def test_production_embedding_contract_applies_add_search_update_modes():
    class Delegate:
        def __init__(self):
            self.calls = []

        def embed(self, text, action):
            self.calls.append((text, action))
            return [3.0, 4.0]

    delegate = Delegate()
    embedding = EncodingContractEmbedding(
        delegate,
        {
            "query_prefix": "query: ",
            "document_prefix": "passage: ",
            "normalize_embeddings": True,
        },
    )
    assert embedding.embed("find", "search") == pytest.approx([0.6, 0.8])
    assert embedding.embed("add", "add") == pytest.approx([0.6, 0.8])
    assert embedding.embed("update", "update") == pytest.approx([0.6, 0.8])
    assert delegate.calls == [
        ("query: find", "search"),
        ("passage: add", "add"),
        ("passage: update", "update"),
    ]


def test_production_cross_encoder_honors_model_revision(monkeypatch):
    import mem0.reranker.sentence_transformer_reranker as module

    captured = {}

    def cross_encoder(model, **kwargs):
        captured.update({"model": model, **kwargs})
        return SimpleNamespace()

    monkeypatch.setattr(module, "CrossEncoder", cross_encoder)
    module.SentenceTransformerReranker(
        {
            "model": "local/model",
            "revision": "immutable-revision",
            "local_files_only": True,
            "device": "cpu",
        }
    )

    assert captured == {
        "model": "local/model",
        "device": "cpu",
        "revision": "immutable-revision",
        "local_files_only": True,
    }

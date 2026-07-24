import hashlib
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.configs.base import BackgroundTaskConfig
from mem0.exceptions import LLMError
from mem0.memory import main as memory_main
from mem0.memory.background_worker import BackgroundWorkerManager
from mem0.memory.main import (
    AsyncMemory,
    Memory,
    _longterm_memory_hash,
    _longterm_memory_id,
    _normalize_memory_text,
    _parse_extracted_memories,
)
from mem0.memory.storage import SQLiteManager


MESSAGES = [
    {"role": "user", "content": "I prefer long-term investing."},
    {"role": "assistant", "content": "I will remember that."},
]
FILTERS = {"user_id": "u1", "run_id": "r1"}
METADATA = dict(FILTERS)


class RecordingVectorStore:
    def __init__(self):
        self.rows = {}
        self.insert_calls = 0
        self.insert_error = None

    def search(self, **kwargs):
        return [
            SimpleNamespace(id=memory_id, payload=dict(row["payload"]), score=1.0)
            for memory_id, row in self.rows.items()
        ]

    def get(self, vector_id):
        row = self.rows.get(vector_id)
        if row is None:
            return None
        return SimpleNamespace(id=vector_id, payload=dict(row["payload"]), score=1.0)

    def insert(self, *, vectors, ids, payloads):
        self.insert_calls += 1
        if self.insert_error is not None:
            raise self.insert_error
        for index, memory_id in enumerate(ids):
            self.rows[memory_id] = {
                "vector": vectors[index],
                "payload": dict(payloads[index]),
            }


class PartialWriteThenRecoverStore(RecordingVectorStore):
    """Fail the first batch and the first individual write for fact B."""

    def __init__(self):
        super().__init__()
        self.batch_failed = False
        self.fact_b_failed = False

    def insert(self, *, vectors, ids, payloads):
        self.insert_calls += 1
        if len(ids) > 1 and not self.batch_failed:
            self.batch_failed = True
            raise RuntimeError("initial batch insert failed")
        if payloads[0]["data"] == "fact B" and not self.fact_b_failed:
            self.fact_b_failed = True
            raise RuntimeError("fact B insert failed")
        for index, memory_id in enumerate(ids):
            self.rows[memory_id] = {
                "vector": vectors[index],
                "payload": dict(payloads[index]),
            }


def _memory(db, response, *, async_mode=False):
    memory_class = AsyncMemory if async_mode else Memory
    memory = memory_class.__new__(memory_class)
    memory.config = SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=False, short_term_capacity=2),
        profile=SimpleNamespace(enabled=False, update_on_add=False),
        background=BackgroundTaskConfig(enabled=False),
    )
    memory.db = db
    memory.api_version = "v1.1"
    memory.custom_instructions = None
    memory._bm25_language = None
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._entity_store = None
    memory.vector_store = RecordingVectorStore()
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2]
    memory.embedding_model.embed_batch.side_effect = lambda texts, action: [[0.1, 0.2] for _ in texts]
    memory.llm = MagicMock()
    memory.llm.generate_response.return_value = response
    return memory


def _reserve(db):
    job_id = db.save_messages_and_create_migration_job(
        MESSAGES,
        "run_id=r1&user_id=u1",
        max_messages=0,
        filters=FILTERS,
        metadata=METADATA,
        infer=True,
        prompt=None,
    )
    assert job_id
    return job_id


def _worker(db, memory, *, max_retries=0):
    return BackgroundWorkerManager(
        db,
        BackgroundTaskConfig(
            max_retries=max_retries,
            poll_interval_seconds=0.005,
        ),
        process_midterm=lambda *args: None,
        process_longterm=memory._background_process_longterm,
        process_profile=lambda *args: None,
    )


@pytest.fixture(autouse=True)
def disable_side_effects(monkeypatch):
    monkeypatch.setattr(memory_main, "capture_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        memory_main,
        "extract_entities_batch",
        lambda texts: [[] for _ in texts],
    )
    monkeypatch.setattr(memory_main, "lemmatize_for_bm25", lambda text, language=None: text)


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.asyncio
async def test_valid_empty_longterm_response_is_success(async_mode):
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": []}', async_mode=async_mode)
    try:
        if async_mode:
            result = await memory._process_evicted_long_term_memories(
                MESSAGES,
                METADATA,
                FILTERS,
                source_job_id="job-empty",
            )
        else:
            result = memory._process_evicted_long_term_memories(
                MESSAGES,
                METADATA,
                FILTERS,
                source_job_id="job-empty",
            )

        assert result == []
        memory.embedding_model.embed_batch.assert_not_called()
        assert memory.vector_store.insert_calls == 0
    finally:
        db.close()


def test_valid_empty_response_marks_background_longterm_complete():
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": []}')
    job_id = _reserve(db)
    worker = _worker(db, memory)
    try:
        worker.start()
        worker.wake_migration()

        assert worker.flush(2)
        job = db.get_background_job(job_id)
        assert job["status"] == "succeeded"
        assert job["longterm_done"] is True
        assert db.get_migration_job_messages(job_id) == []
    finally:
        worker.stop(timeout=1)
        db.close()


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not valid json",
        "[]",
        '{"other": []}',
        '{"memory": {}}',
        '{"memory": [{}]}',
    ],
)
def test_strict_longterm_parser_rejects_invalid_responses(response):
    with pytest.raises(LLMError, match="Long-term memory response parsing failed"):
        _parse_extracted_memories(response, strict=True)

    assert _parse_extracted_memories(response, strict=False) == []


def test_longterm_parser_keeps_extract_json_recovery():
    response = 'Here is the result: {"memory": [{"text": "recovered fact"}]} Done.'

    assert _parse_extracted_memories(response, strict=True) == [{"text": "recovered fact"}]


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.asyncio
async def test_sync_and_async_strict_parsing_match(async_mode):
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": "not-a-list"}', async_mode=async_mode)
    try:
        with pytest.raises(LLMError, match="'memory' must be a list"):
            if async_mode:
                await memory._process_evicted_long_term_memories(
                    MESSAGES,
                    METADATA,
                    FILTERS,
                    source_job_id="job-invalid",
                )
            else:
                memory._process_evicted_long_term_memories(
                    MESSAGES,
                    METADATA,
                    FILTERS,
                    source_job_id="job-invalid",
                )
    finally:
        db.close()


def test_invalid_json_enters_retry_then_can_succeed(monkeypatch):
    monkeypatch.setattr("mem0.memory.background_worker._RETRY_DELAYS_SECONDS", (0.05, 0.05, 0.05))
    db = SQLiteManager(":memory:")
    memory = _memory(db, "not-json")
    memory.llm.generate_response.side_effect = ["not-json", '{"memory": []}']
    job_id = _reserve(db)
    worker = _worker(db, memory, max_retries=1)
    try:
        worker.start()
        worker.wake_migration()
        deadline = time.monotonic() + 1
        while db.get_background_job(job_id)["status"] != "retry" and time.monotonic() < deadline:
            time.sleep(0.005)

        assert db.get_background_job(job_id)["status"] == "retry"
        assert db.get_migration_job_messages(job_id)
        assert worker.flush(2)
        assert db.get_background_job(job_id)["status"] == "succeeded"
    finally:
        worker.stop(timeout=1)
        db.close()


def test_invalid_json_exhaustion_uses_longterm_degradation():
    db = SQLiteManager(":memory:")
    memory = _memory(db, "not-json")
    job_id = _reserve(db)
    worker = _worker(db, memory)
    try:
        worker.start()
        worker.wake_migration()

        assert worker.flush(2)
        assert db.get_background_job(job_id)["status"] == "succeeded_degraded"
        assert db.get_migration_job_messages(job_id) == []
        fallback_rows = [
            row
            for row in memory.vector_store.rows.values()
            if row["payload"].get("memory_type") == "raw_fallback"
        ]
        assert len(fallback_rows) == 1
    finally:
        worker.stop(timeout=1)
        db.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.asyncio
async def test_batch_embedding_failure_falls_back_to_individual_embeddings(async_mode):
    db = SQLiteManager(":memory:")
    memory = _memory(
        db,
        '{"memory": [{"text": "fact one"}, {"text": "fact two"}]}',
        async_mode=async_mode,
    )
    memory.embedding_model.embed_batch.side_effect = RuntimeError("batch unavailable")
    try:
        if async_mode:
            result = await memory._process_evicted_long_term_memories(
                MESSAGES,
                METADATA,
                FILTERS,
                source_job_id="job-embed-fallback",
            )
        else:
            result = memory._process_evicted_long_term_memories(
                MESSAGES,
                METADATA,
                FILTERS,
                source_job_id="job-embed-fallback",
            )

        assert len(result) == 2
        assert memory.vector_store.insert_calls == 1
    finally:
        db.close()


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.asyncio
async def test_all_fact_embeddings_failing_raises_before_any_insert(async_mode):
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": [{"text": "fact one"}]}', async_mode=async_mode)
    memory.embedding_model.embed_batch.side_effect = RuntimeError("batch unavailable")

    def embed(text, action):
        if action == "search":
            return [0.1, 0.2]
        raise RuntimeError("embed unavailable")

    memory.embedding_model.embed.side_effect = embed
    try:
        with pytest.raises(RuntimeError, match="Failed to embed 1 extracted memories"):
            if async_mode:
                await memory._process_evicted_long_term_memories(
                    MESSAGES,
                    METADATA,
                    FILTERS,
                    source_job_id="job-no-embedding",
                )
            else:
                memory._process_evicted_long_term_memories(
                    MESSAGES,
                    METADATA,
                    FILTERS,
                    source_job_id="job-no-embedding",
                )
        assert memory.vector_store.insert_calls == 0
    finally:
        db.close()


def test_partial_fact_embedding_failure_does_not_write_partial_results():
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": [{"text": "fact one"}, {"text": "fact two"}]}')
    memory.embedding_model.embed_batch.side_effect = RuntimeError("batch unavailable")

    def embed(text, action):
        if action == "search" or text == "fact one":
            return [0.1, 0.2]
        raise RuntimeError("second fact failed")

    memory.embedding_model.embed.side_effect = embed
    try:
        with pytest.raises(RuntimeError, match="Failed to embed 1 extracted memories"):
            memory._process_evicted_long_term_memories(
                MESSAGES,
                METADATA,
                FILTERS,
                source_job_id="job-partial-embedding",
            )
        assert memory.vector_store.insert_calls == 0
        assert memory.vector_store.rows == {}
    finally:
        db.close()


def test_embedding_failure_enters_retry_then_succeeds(monkeypatch):
    monkeypatch.setattr("mem0.memory.background_worker._RETRY_DELAYS_SECONDS", (0.05, 0.05, 0.05))
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": [{"text": "retry fact"}]}')
    memory.embedding_model.embed_batch.side_effect = [
        RuntimeError("batch unavailable"),
        [[0.3, 0.4]],
    ]
    individual_attempts = 0

    def embed(text, action):
        nonlocal individual_attempts
        if action == "search":
            return [0.1, 0.2]
        individual_attempts += 1
        raise RuntimeError("individual embedding unavailable")

    memory.embedding_model.embed.side_effect = embed
    job_id = _reserve(db)
    worker = _worker(db, memory, max_retries=1)
    try:
        worker.start()
        worker.wake_migration()
        deadline = time.monotonic() + 1
        while db.get_background_job(job_id)["status"] != "retry" and time.monotonic() < deadline:
            time.sleep(0.005)

        assert db.get_background_job(job_id)["status"] == "retry"
        assert db.get_migration_job_messages(job_id)
        assert worker.flush(2)
        assert db.get_background_job(job_id)["status"] == "succeeded"
        assert individual_attempts == 1
    finally:
        worker.stop(timeout=1)
        db.close()


def test_hash_duplicate_and_existing_deterministic_id_need_no_fact_embedding():
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": [{"text": "duplicate fact"}]}')
    duplicate_hash = hashlib.md5(b"duplicate fact").hexdigest()
    memory.vector_store.rows["existing-hash"] = {
        "vector": [0.1, 0.2],
        "payload": {"data": "duplicate fact", "hash": duplicate_hash},
    }
    try:
        assert (
            memory._process_evicted_long_term_memories(
                MESSAGES,
                METADATA,
                FILTERS,
                source_job_id="job-hash-duplicate",
            )
            == []
        )
        memory.embedding_model.embed_batch.assert_not_called()

        memory.vector_store.rows.clear()
        deterministic_id = _longterm_memory_id("job-existing-id", "duplicate fact")
        memory.vector_store.rows[deterministic_id] = {
            "vector": [0.1, 0.2],
            "payload": {"data": "duplicate fact", "hash": duplicate_hash},
        }
        assert (
            memory._process_evicted_long_term_memories(
                MESSAGES,
                METADATA,
                FILTERS,
                source_job_id="job-existing-id",
            )
            == []
        )
        memory.embedding_model.embed_batch.assert_not_called()
        assert db.get_history(deterministic_id)
    finally:
        db.close()


def test_retry_repairs_history_without_repeating_vector_write():
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": [{"text": "persisted fact"}]}')
    original_add_history = db.add_history
    db.add_history = MagicMock(side_effect=RuntimeError("history unavailable"))
    try:
        with pytest.raises(RuntimeError, match="history unavailable"):
            memory._process_evicted_long_term_memories(
                MESSAGES,
                METADATA,
                FILTERS,
                source_job_id="job-history-retry",
            )
        assert memory.vector_store.insert_calls == 1

        db.add_history = original_add_history
        assert (
            memory._process_evicted_long_term_memories(
                MESSAGES,
                METADATA,
                FILTERS,
                source_job_id="job-history-retry",
            )
            == []
        )
        assert memory.vector_store.insert_calls == 1
        deterministic_id = _longterm_memory_id("job-history-retry", "persisted fact")
        assert db.get_history(deterministic_id)
    finally:
        db.close()


@pytest.mark.parametrize("fallback_fails", [False, True])
def test_embedding_exhaustion_degrades_or_marks_dead(fallback_fails):
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": [{"text": "fact cannot embed"}]}')
    memory.embedding_model.embed_batch.side_effect = RuntimeError("batch unavailable")

    def embed(text, action):
        if action == "search":
            return [0.1, 0.2]
        if text == "fact cannot embed":
            raise RuntimeError("fact embedding unavailable")
        return [0.3, 0.4]

    memory.embedding_model.embed.side_effect = embed
    if fallback_fails:
        memory.vector_store.insert_error = RuntimeError("fallback vector write unavailable")
    job_id = _reserve(db)
    worker = _worker(db, memory)
    try:
        worker.start()
        worker.wake_migration()

        assert worker.flush(2)
        expected_status = "dead" if fallback_fails else "succeeded_degraded"
        assert db.get_background_job(job_id)["status"] == expected_status
        if fallback_fails:
            assert db.get_migration_job_messages(job_id)
            assert {row["status"] for row in db.get_migration_job_messages(job_id)} == {"failed"}
        else:
            assert db.get_migration_job_messages(job_id) == []
    finally:
        worker.stop(timeout=1)
        db.close()


def test_content_based_ids_are_stable_when_fact_order_changes():
    source_job_id = "job-reordered"
    first_response = ["fact A", "fact B"]
    second_response = ["fact B", "fact A"]

    first_ids = {text: _longterm_memory_id(source_job_id, text) for text in first_response}
    second_ids = {text: _longterm_memory_id(source_job_id, text) for text in second_response}

    assert first_ids == second_ids
    assert first_ids["fact A"] != first_ids["fact B"]


def test_partial_write_then_reordered_retry_preserves_all_facts(monkeypatch):
    monkeypatch.setattr("mem0.memory.background_worker._RETRY_DELAYS_SECONDS", (0.05, 0.05, 0.05))
    db = SQLiteManager(":memory:")
    memory = _memory(db, None)
    memory.vector_store = PartialWriteThenRecoverStore()
    memory.llm.generate_response.side_effect = [
        '{"memory": [{"text": "fact A"}, {"text": "fact B"}]}',
        '{"memory": [{"text": "fact B"}, {"text": "fact A"}]}',
    ]
    job_id = _reserve(db)
    worker = _worker(db, memory, max_retries=1)
    try:
        worker.start()
        worker.wake_migration()
        deadline = time.monotonic() + 1
        while db.get_background_job(job_id)["status"] != "retry" and time.monotonic() < deadline:
            time.sleep(0.005)

        assert db.get_background_job(job_id)["status"] == "retry"
        assert set(row["payload"]["data"] for row in memory.vector_store.rows.values()) == {"fact A"}
        assert db.get_migration_job_messages(job_id)

        assert worker.flush(2)
        assert db.get_background_job(job_id)["status"] == "succeeded"
        assert db.get_migration_job_messages(job_id) == []
        assert len(memory.vector_store.rows) == 2
        assert {
            memory_id: row["payload"]["data"] for memory_id, row in memory.vector_store.rows.items()
        } == {
            _longterm_memory_id(job_id, "fact A"): "fact A",
            _longterm_memory_id(job_id, "fact B"): "fact B",
        }
    finally:
        worker.stop(timeout=1)
        db.close()


def test_history_failure_then_reordered_retry_repairs_correct_histories(monkeypatch):
    monkeypatch.setattr("mem0.memory.background_worker._RETRY_DELAYS_SECONDS", (0.05, 0.05, 0.05))
    db = SQLiteManager(":memory:")
    memory = _memory(db, None)
    memory.llm.generate_response.side_effect = [
        '{"memory": [{"text": "fact A"}, {"text": "fact B"}]}',
        '{"memory": [{"text": "fact B"}, {"text": "fact A"}]}',
    ]
    original_add_history = db.add_history
    failed_once = False

    def add_history_with_one_failure(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("history unavailable")
        return original_add_history(*args, **kwargs)

    db.add_history = MagicMock(side_effect=add_history_with_one_failure)
    job_id = _reserve(db)
    worker = _worker(db, memory, max_retries=1)
    try:
        worker.start()
        worker.wake_migration()
        deadline = time.monotonic() + 1
        while db.get_background_job(job_id)["status"] != "retry" and time.monotonic() < deadline:
            time.sleep(0.005)

        assert db.get_background_job(job_id)["status"] == "retry"
        assert memory.vector_store.insert_calls == 1
        assert len(memory.vector_store.rows) == 2

        assert worker.flush(2)
        assert db.get_background_job(job_id)["status"] == "succeeded"
        assert memory.vector_store.insert_calls == 1
        for fact in ("fact A", "fact B"):
            memory_id = _longterm_memory_id(job_id, fact)
            history = db.get_history(memory_id)
            assert len(history) == 1
            assert history[0]["new_memory"] == fact
    finally:
        worker.stop(timeout=1)
        db.close()


def test_duplicate_facts_in_one_job_create_one_memory():
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": [{"text": "same fact"}, {"text": "same fact"}]}')
    try:
        result = memory._process_evicted_long_term_memories(
            MESSAGES,
            METADATA,
            FILTERS,
            source_job_id="job-duplicate-facts",
        )

        assert len(result) == 1
        assert memory.vector_store.insert_calls == 1
        assert list(memory.vector_store.rows) == [_longterm_memory_id("job-duplicate-facts", "same fact")]
    finally:
        db.close()


def test_whitespace_normalization_produces_the_same_hash_and_id():
    plain = "fact A"
    padded = "  fact   A  "
    source_job_id = "job-whitespace"

    assert _normalize_memory_text(padded) == plain
    assert _longterm_memory_hash(plain) == _longterm_memory_hash(padded)
    assert _longterm_memory_id(source_job_id, plain) == _longterm_memory_id(source_job_id, padded)

    db = SQLiteManager(":memory:")
    memory = _memory(db, f'{{"memory": [{{"text": "{padded}"}}]}}')
    try:
        memory._process_evicted_long_term_memories(
            MESSAGES,
            METADATA,
            FILTERS,
            source_job_id=source_job_id,
        )
        memory.llm.generate_response.return_value = '{"memory": [{"text": "fact A"}]}'
        memory._process_evicted_long_term_memories(
            MESSAGES,
            METADATA,
            FILTERS,
            source_job_id=source_job_id,
        )

        expected_id = _longterm_memory_id(source_job_id, plain)
        assert set(memory.vector_store.rows) == {expected_id}
        assert memory.vector_store.insert_calls == 1
        assert memory.vector_store.rows[expected_id]["payload"]["data"] == padded
        assert memory.vector_store.rows[expected_id]["payload"]["hash"] == _longterm_memory_hash(plain)
    finally:
        db.close()


def test_existing_deterministic_id_hash_mismatch_retries_without_overwrite(caplog):
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": [{"text": "fact A"}]}')
    job_id = _reserve(db)
    memory_id = _longterm_memory_id(job_id, "fact A")
    wrong_payload = {
        "data": "different fact",
        "hash": _longterm_memory_hash("different fact"),
        "source_job_id": job_id,
    }
    memory.vector_store.rows[memory_id] = {
        "vector": [9.0, 9.0],
        "payload": dict(wrong_payload),
    }
    worker = _worker(db, memory, max_retries=1)
    try:
        worker.start()
        worker.wake_migration()
        deadline = time.monotonic() + 1
        while db.get_background_job(job_id)["status"] != "retry" and time.monotonic() < deadline:
            time.sleep(0.005)

        job = db.get_background_job(job_id)
        assert job["status"] == "retry"
        assert job["longterm_done"] is False
        assert db.get_migration_job_messages(job_id)
        assert memory.vector_store.rows[memory_id]["payload"] == wrong_payload
        assert str(job_id) in caplog.text
        assert memory_id in caplog.text
    finally:
        worker.stop(timeout=1)
        db.close()


@pytest.mark.asyncio
async def test_sync_and_async_paths_generate_the_same_content_based_id():
    sync_db = SQLiteManager(":memory:")
    async_db = SQLiteManager(":memory:")
    sync_memory = _memory(sync_db, '{"memory": [{"text": "shared fact"}]}')
    async_memory = _memory(async_db, '{"memory": [{"text": "shared fact"}]}', async_mode=True)
    source_job_id = "job-sync-async-id"
    try:
        sync_memory._process_evicted_long_term_memories(
            MESSAGES,
            METADATA,
            FILTERS,
            source_job_id=source_job_id,
        )
        await async_memory._process_evicted_long_term_memories(
            MESSAGES,
            METADATA,
            FILTERS,
            source_job_id=source_job_id,
        )

        expected_id = _longterm_memory_id(source_job_id, "shared fact")
        assert set(sync_memory.vector_store.rows) == {expected_id}
        assert set(async_memory.vector_store.rows) == {expected_id}
    finally:
        sync_db.close()
        async_db.close()


def test_legacy_index_id_is_reused_by_source_job_hash_and_history_repaired():
    db = SQLiteManager(":memory:")
    memory = _memory(db, '{"memory": [{"text": "legacy fact"}]}')
    job_id = _reserve(db)
    legacy_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:longterm:{job_id}:0"))
    memory.vector_store.rows[legacy_id] = {
        "vector": [0.1, 0.2],
        "payload": {
            "data": "legacy fact",
            "hash": _longterm_memory_hash("legacy fact"),
            "source_job_id": job_id,
        },
    }
    worker = _worker(db, memory)
    try:
        worker.start()
        worker.wake_migration()

        assert worker.flush(2)
        assert db.get_background_job(job_id)["status"] == "succeeded"
        assert db.get_migration_job_messages(job_id) == []
        assert set(memory.vector_store.rows) == {legacy_id}
        assert memory.vector_store.insert_calls == 0
        history = db.get_history(legacy_id)
        assert len(history) == 1
        assert history[0]["new_memory"] == "legacy fact"
        assert _longterm_memory_id(job_id, "legacy fact") not in memory.vector_store.rows
    finally:
        worker.stop(timeout=1)
        db.close()

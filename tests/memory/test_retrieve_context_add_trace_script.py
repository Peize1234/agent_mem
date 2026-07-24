import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from finance_memory_single_session_package import run_retrieve_context_add_trace_test as trace_script
from mem0.memory.storage import SQLiteManager


def _write_dataset(path: Path, *, second_pairs: int = 5, fourth_pairs: int = 5, include_user=True):
    def session(session_id, pair_count):
        turns = []
        for index in range(1, pair_count + 1):
            turns.extend(
                [
                    {
                        "turn_id": f"{session_id}-U{index}",
                        "role": "user",
                        "content": f"{session_id}-question-{index}",
                    },
                    {
                        "turn_id": f"{session_id}-A{index}",
                        "role": "assistant",
                        "content": f"{session_id}-reference-answer-{index}",
                    },
                ]
            )
        return {"session_id": session_id, "timestamp": "2026-01-01T00:00:00+08:00", "turns": turns}

    data = {
        "sessions": [
            session("S001", 0),
            session("S002", second_pairs),
            session("S003", 0),
            session("S004", fourth_pairs),
        ]
    }
    if include_user:
        data["user"] = {"user_id": "dataset-user"}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class FakeLLM:
    def __init__(self, events=None, *, error=None):
        self.events = events if events is not None else []
        self.error = error
        self.call_count = 0

    def generate_response(self, **kwargs):
        self.events.append("llm")
        self.call_count += 1
        if self.error is not None:
            raise self.error
        return f"raw-response-{self.call_count}"


class FakeMemory:
    def __init__(self, events):
        self.events = events
        self.llm = FakeLLM(events)
        self.retrieve_calls = []
        self.add_calls = []
        self.short_term = {"S002": [], "S004": []}
        self.shared_profile = {"risk_level": "balanced"}

    def retrieve_context(self, query, **kwargs):
        session_id = kwargs["session_id"]
        self.events.append("retrieve")
        context = {
            "user_id": kwargs["user_id"],
            "session_id": session_id,
            "query": query,
            "profile": self.shared_profile,
            "short_term_messages": list(self.short_term[session_id]),
            "retrieved_memories": [{"memory": f"retrieved-for-{session_id}", "score": 0.9}],
        }
        self.retrieve_calls.append({"query": query, **kwargs, "context": context})
        return context

    def add(self, messages, **kwargs):
        self.events.append("add")
        self.add_calls.append({"messages": messages, **kwargs})
        self.llm.generate_response(messages=[{"role": "system", "content": "add-stage-prompt"}])
        self.short_term[kwargs["run_id"]].extend(messages)
        return {"results": [{"id": f"memory-{len(self.add_calls)}", "event": "ADD"}]}


def _fake_snapshot(events):
    def collect(memory, user_id):
        events.append("snapshot")
        return {
            "timestamp": "2026-07-21T00:00:00+00:00",
            "sqlite": {"tables": {"messages": []}},
            "long_term_memory": [],
            "midterm_memory": {"sessions": [], "pages": []},
            "entity_store": {"initialized": False, "records": []},
            "profile_view": {"user_id": user_id, "profile": memory.shared_profile},
            "warnings": [],
        }

    return collect


def _runner(tmp_path, memory, selected_sessions, turns, recorder, *, test_mode, snapshot_collector):
    return trace_script.RetrieveContextAddTraceRunner(
        memory=memory,
        turns=turns,
        selected_sessions=selected_sessions,
        user_id=turns[0]["user_id"],
        recorder=recorder,
        test_mode=test_mode,
        output_root=tmp_path / "trace-output",
        dataset_path=tmp_path / "dataset.json",
        memory_config_summary={"llm": {"api_key": "do-not-log", "model": "mock-model"}},
        snapshot_collector=snapshot_collector,
        run_timestamp="20260721_153000",
    )


def test_selects_second_and_fourth_sessions_in_original_order(tmp_path):
    dataset_path = _write_dataset(tmp_path / "dataset.json")

    sessions, turns, user_id = trace_script.load_selected_turns(dataset_path)

    assert [session["session_id"] for session in sessions] == ["S002", "S004"]
    assert [turn["query"] for turn in turns] == [
        *(f"S002-question-{index}" for index in range(1, 6)),
        *(f"S004-question-{index}" for index in range(1, 6)),
    ]
    assert [turn["global_turn_index"] for turn in turns] == list(range(1, 11))
    assert [turn["session_turn_index"] for turn in turns] == [1, 2, 3, 4, 5, 1, 2, 3, 4, 5]
    assert user_id == "dataset-user"


def test_actual_finance_dataset_selects_s002_and_s004_with_ten_questions():
    sessions, turns, user_id = trace_script.load_selected_turns(trace_script.DEFAULT_DATASET_PATH)

    assert [session["session_id"] for session in sessions] == ["S002", "S004"]
    assert len(turns) == 10
    assert turns[0]["query"].startswith("我计划在明年4月前后买房")
    assert turns[5]["query"].startswith("为了方便估算现金流")
    assert user_id == "invest_user_001"


def test_uses_fixed_user_when_dataset_has_no_user_id(tmp_path):
    dataset_path = _write_dataset(tmp_path / "dataset.json", include_user=False)

    _, turns, user_id = trace_script.load_selected_turns(dataset_path)

    assert user_id == trace_script.DEFAULT_USER_ID
    assert {turn["user_id"] for turn in turns} == {trace_script.DEFAULT_USER_ID}


def test_rejects_selected_sessions_with_non_ten_question_count(tmp_path):
    dataset_path = _write_dataset(tmp_path / "dataset.json", fourth_pairs=4)

    with pytest.raises(ValueError, match="Expected 10.*actual count: 9"):
        trace_script.load_selected_turns(dataset_path)


def test_test_mode_precedence():
    assert trace_script.resolve_test_mode(True, "false") is True
    assert trace_script.resolve_test_mode(False, "true") is False
    assert trace_script.resolve_test_mode(None, "true") is True
    assert trace_script.resolve_test_mode(None, "false") is False
    assert trace_script.resolve_test_mode(None, None) is False


def test_non_test_mode_creates_no_trace_files_or_debug_output(tmp_path, capsys, monkeypatch):
    dataset_path = _write_dataset(tmp_path / "dataset.json")
    selected_sessions, turns, _ = trace_script.load_selected_turns(dataset_path)
    events = []
    memory = FakeMemory(events)
    recorder = trace_script.TraceRecorder(False)
    monkeypatch.setattr(
        trace_script,
        "render_database_snapshot",
        lambda *_: pytest.fail("database rendering must not run outside test mode"),
    )
    monkeypatch.setattr(
        trace_script,
        "render_llm_messages",
        lambda *_: pytest.fail("prompt rendering must not run outside test mode"),
    )

    runner = _runner(
        tmp_path,
        memory,
        selected_sessions,
        turns,
        recorder,
        test_mode=False,
        snapshot_collector=lambda *_: pytest.fail("snapshot must not run outside test mode"),
    )
    manifest = runner.run()

    assert manifest["completed_turn_count"] == 10
    assert not (tmp_path / "trace-output").exists()
    assert capsys.readouterr().out == ""
    assert len(memory.add_calls) == 10


def test_traced_run_preserves_order_writes_each_turn_and_records_context(tmp_path, monkeypatch):
    dataset_path = _write_dataset(tmp_path / "dataset.json")
    selected_sessions, turns, _ = trace_script.load_selected_turns(dataset_path)
    events = []
    memory = FakeMemory(events)
    recorder = trace_script.TraceRecorder(True)
    memory.llm = trace_script.TracedLLM(memory.llm, recorder, provider="mock", model="mock-model")
    runner = _runner(
        tmp_path,
        memory,
        selected_sessions,
        turns,
        recorder,
        test_mode=True,
        snapshot_collector=_fake_snapshot(events),
    )

    original_atomic_write = trace_script.atomic_write_text
    turn_write_counts = []

    def recording_atomic_write(path, content):
        if path.name[:2].isdigit() and "session" in path.name:
            turn_write_counts.append((path.name, len(memory.add_calls)))
        original_atomic_write(path, content)

    monkeypatch.setattr(trace_script, "atomic_write_text", recording_atomic_write)
    manifest = runner.run()

    output_dir = tmp_path / "trace-output" / "20260721_153000"
    turn_files = sorted(output_dir.glob("[0-9][0-9]_session*_turn*.md"))
    assert len(turn_files) == 10
    assert [path.name for path in turn_files] == [
        *(f"{index:02d}_session2_turn{index}.md" for index in range(1, 6)),
        *(f"{index:02d}_session4_turn{index - 5}.md" for index in range(6, 11)),
    ]
    assert turn_write_counts == [(path.name, index) for index, path in enumerate(turn_files, start=1)]
    assert (output_dir / "00_initial_database_snapshot.md").exists()
    assert (output_dir / "11_final_database_snapshot.md").exists()
    assert (output_dir / "run_manifest.json").exists()

    assert events == ["snapshot"] + ["snapshot", "retrieve", "llm", "add", "llm", "snapshot"] * 10 + ["snapshot"]
    assert all([message["role"] for message in call["messages"]] == ["user", "assistant"] for call in memory.add_calls)
    assert [call["run_id"] for call in memory.add_calls] == ["S002"] * 5 + ["S004"] * 5
    assert {call["user_id"] for call in memory.add_calls} == {"dataset-user"}

    first_s004_context = memory.retrieve_calls[5]["context"]
    assert first_s004_context["short_term_messages"] == []
    assert all(call["context"]["profile"] is memory.shared_profile for call in memory.retrieve_calls)
    assert memory.retrieve_calls[1]["context"]["short_term_messages"]

    answer_calls = [call for call in recorder.calls if call["phase"] == "answer_generation"]
    add_calls = [call for call in recorder.calls if call["phase"] == "add"]
    assert len(answer_calls) == len(add_calls) == 10
    assert not [call for call in recorder.calls if call["phase"] == "retrieve_context"]
    prompt_text = json.dumps(answer_calls[1]["messages"], ensure_ascii=False)
    assert "risk_level" in prompt_text
    assert "S002-question-1" in prompt_text
    assert "retrieved-for-S002" in prompt_text
    assert "reference-answer" not in prompt_text

    first_file = turn_files[0].read_text(encoding="utf-8")
    assert "## 3. 回答前数据库完整快照" in first_file
    assert "## 12. 回答后数据库完整快照" in first_file
    assert first_file.count("## SQLite 数据库") == 2
    assert "## 最终回答阶段 LLM 调用详情" in first_file
    assert "## 本轮分层记忆迁移" in first_file
    assert "| 本轮淘汰消息数量 | 0 |" in first_file
    assert "| Source Type | answer_generation |" in first_file
    assert "本阶段未调用 LLM" in first_file
    assert "S002-reference-answer-1" in first_file
    initial_file = (output_dir / "00_initial_database_snapshot.md").read_text(encoding="utf-8")
    final_file = (output_dir / "11_final_database_snapshot.md").read_text(encoding="utf-8")
    assert "## SQLite 数据库" in initial_file
    assert "## SQLite 数据库" in final_file
    assert '```json\n{\n  "timestamp"' not in first_file + initial_file + final_file
    manifest_on_disk = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest_on_disk["completed_turn_count"] == 10
    assert manifest_on_disk["selected_session_indexes"] == [1, 3]
    assert manifest_on_disk["selected_session_ids"] == ["S002", "S004"]
    assert "do-not-log" not in (output_dir / "run_manifest.json").read_text(encoding="utf-8")
    assert manifest["failed_turns"] == []


def test_traced_llm_records_all_phases_and_errors():
    recorder = trace_script.TraceRecorder(False)
    wrapped = trace_script.TracedLLM(FakeLLM(), recorder, provider="mock", model="model")

    for phase in ("retrieve_context", "answer_generation", "add"):
        with recorder.phase(phase):
            wrapped.generate_response(messages=[{"role": "user", "content": phase}])

    failing = trace_script.TracedLLM(
        FakeLLM(error=RuntimeError("LLM unavailable")),
        recorder,
        provider="mock",
        model="model",
    )
    with pytest.raises(RuntimeError, match="LLM unavailable"):
        with recorder.phase("add"):
            failing.generate_response(messages=[{"role": "user", "content": "complete prompt"}])

    assert [call["phase"] for call in recorder.calls[:3]] == [
        "retrieve_context",
        "answer_generation",
        "add",
    ]
    assert recorder.calls[-1]["messages"] == [{"role": "user", "content": "complete prompt"}]
    assert recorder.calls[-1]["error"] == {"type": "RuntimeError", "message": "LLM unavailable"}


@pytest.mark.asyncio
async def test_traced_llm_uses_real_async_method_only_when_available():
    class AsyncLLM:
        async def generate_response_async(self, **kwargs):
            await asyncio.sleep(0)
            return "async-response"

    recorder = trace_script.TraceRecorder(False)
    wrapped = trace_script.TracedLLM(AsyncLLM(), recorder, provider="mock", model="model")

    with recorder.phase("add"):
        response = await wrapped.generate_response_async(messages=[{"role": "user", "content": "prompt"}])

    assert response == "async-response"
    assert recorder.calls[0]["raw_response"] == "async-response"
    with pytest.raises(AttributeError):
        getattr(trace_script.TracedLLM(FakeLLM(), recorder, provider="mock", model="model"), "generate_response_async")


def test_snapshot_enumerates_all_sqlite_business_tables(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    try:
        db.connection.execute("CREATE TABLE trace_extra (id TEXT, value TEXT)")
        db.connection.execute("INSERT INTO trace_extra VALUES ('1', 'kept')")
        db.connection.commit()

        class Row:
            id = "vector-1"
            payload = {"data": "long-term memory"}
            metadata = {"source": "test"}
            score = 0.8

        memory = SimpleNamespace(
            db=db,
            vector_store=SimpleNamespace(list=lambda **_: ([Row()], None)),
            config=SimpleNamespace(midterm=SimpleNamespace(enabled=False)),
            _entity_store=None,
            get_profile=lambda user_id, include_metadata: {
                "user_id": user_id,
                "profile": {"risk_level": {"value": "balanced"}},
            },
        )

        snapshot = trace_script.collect_full_database_snapshot(memory, "user-1")

        sqlite_tables = snapshot["sqlite"]["tables"]
        expected_business_tables = {
            row[0]
            for row in db.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert set(sqlite_tables) == expected_business_tables
        assert sqlite_tables["trace_extra"] == [{"id": "1", "value": "kept"}]
        assert snapshot["long_term_memory"][0]["id"] == "vector-1"
        assert snapshot["profile_view"]["profile"]["risk_level"]["value"] == "balanced"
        assert snapshot["entity_store"] == {"initialized": False, "records": []}
    finally:
        db.close()


def test_safe_serialization_supports_common_types_and_redacts_secrets(tmp_path):
    class Color(Enum):
        RED = "red"

    @dataclass
    class Payload:
        created_at: datetime
        path: Path

    value = {
        "api_key": "top-secret-key",
        "authorization": "Bearer secret",
        "payload": Payload(datetime(2026, 7, 21), tmp_path),
        "values": ({1, 2}, Color.RED),
    }

    serialized = trace_script.make_json_serializable(value)
    rendered = json.dumps(serialized)

    assert "top-secret-key" not in rendered
    assert "Bearer secret" not in rendered
    assert serialized["api_key"] == "***REDACTED***"
    assert serialized["payload"]["path"] == str(tmp_path)
    assert serialized["values"][1] == "red"


def test_llm_message_and_string_response_rendering_preserves_real_newlines_and_fences():
    messages = [
        {
            "role": "system",
            "content": "第一行\n```python\nprint('still inside prompt')\n```\n最后一行",
        }
    ]
    rendered_messages = trace_script.render_llm_messages(messages)
    rendered_call = trace_script._render_llm_calls(
        [
            {
                "call_index": 1,
                "phase": "answer_generation",
                "source_type": "answer_generation",
                "caller_module": "trace_script",
                "caller_class": None,
                "caller_method": "generate_final_answer",
                "caller_file": "/tmp/trace_script.py",
                "caller_line": 10,
                "provider": "mock",
                "model": "mock-model",
                "timestamp": "2026-07-21T00:00:00+00:00",
                "messages": messages,
                "response_format": None,
                "other_safe_kwargs": {},
                "raw_response": "回答第一行\n回答第二行",
                "error": None,
            }
        ]
    )

    assert "第一行\n```python" in rendered_messages
    assert r"第一行\n```python" not in rendered_messages
    assert "````text\n第一行" in rendered_messages
    assert "```\n最后一行\n````" in rendered_messages
    assert "回答第一行\n回答第二行" in rendered_call
    assert r"回答第一行\n回答第二行" not in rendered_call


@pytest.mark.parametrize(
    ("caller", "phase", "expected_source"),
    [
        (
            {
                "caller_module": "mem0.memory.main",
                "caller_class": "Memory",
                "caller_method": "_process_evicted_long_term_memories",
                "caller_file": "/repo/mem0/memory/main.py",
                "caller_line": 1192,
            },
            "add",
            "long_term_memory",
        ),
        (
            {
                "caller_module": "mem0.memory.midterm_updater",
                "caller_class": "MidTermUpdater",
                "caller_method": "_summarize_page",
                "caller_file": "/repo/mem0/memory/midterm_updater.py",
                "caller_line": 71,
            },
            "add",
            "midterm_memory",
        ),
        (
            {
                "caller_module": "mem0.memory.profile_updater",
                "caller_class": "ProfileUpdater",
                "caller_method": "generate_update_plan",
                "caller_file": "/repo/mem0/memory/profile_updater.py",
                "caller_line": 77,
            },
            "add",
            "user_profile",
        ),
        (
            {
                "caller_module": "some_extension",
                "caller_class": "Extension",
                "caller_method": "invoke",
                "caller_file": "/repo/some_extension.py",
                "caller_line": 42,
            },
            "add",
            "unknown",
        ),
    ],
)
def test_traced_llm_records_automatically_classified_caller(monkeypatch, caller, phase, expected_source):
    monkeypatch.setattr(trace_script, "detect_llm_caller", lambda: caller)
    recorder = trace_script.TraceRecorder(True)
    wrapped = trace_script.TracedLLM(FakeLLM(), recorder, provider="mock", model="mock-model")

    with recorder.phase(phase):
        wrapped.generate_response(messages=[{"role": "user", "content": "prompt"}])

    assert recorder.calls[0]["caller_method"] == caller["caller_method"]
    assert recorder.calls[0]["source_type"] == expected_source


def test_explicit_answer_source_takes_priority_over_stack_detection(monkeypatch):
    monkeypatch.setattr(trace_script, "detect_llm_caller", lambda: pytest.fail("explicit source must skip stack"))
    recorder = trace_script.TraceRecorder(True)
    wrapped = trace_script.TracedLLM(FakeLLM(), recorder, provider="mock", model="mock-model")

    with (
        recorder.phase("answer_generation"),
        recorder.llm_source(
            source_type="answer_generation",
            caller_method="generate_final_answer",
        ),
    ):
        wrapped.generate_response(messages=[{"role": "user", "content": "prompt"}])

    assert recorder.calls[0]["source_type"] == "answer_generation"
    assert recorder.calls[0]["caller_method"] == "generate_final_answer"


def test_source_classifier_supports_retrieve_and_procedural_memory():
    assert (
        trace_script.classify_llm_source("mem0.memory.main", "Memory", "_create_procedural_memory", "add")
        == "procedural_memory"
    )
    assert trace_script.classify_llm_source("mem0.memory.search", "Memory", "search", "retrieve_context") == (
        "retrieve_context"
    )


def test_disabled_traced_llm_does_not_inspect_stack(monkeypatch):
    monkeypatch.setattr(trace_script.inspect, "stack", lambda: pytest.fail("inspect.stack must not run"))
    recorder = trace_script.TraceRecorder(False)
    wrapped = trace_script.TracedLLM(FakeLLM(), recorder, provider="mock", model="mock-model")

    response = wrapped.generate_response(messages=[{"role": "user", "content": "prompt"}])

    assert response == "raw-response-1"
    assert recorder.calls[0]["source_type"] == "unknown"


def test_test_eviction_trace_records_messages_and_layer_triggers():
    class LayeredMemory:
        def _save_short_term_messages(self, messages, session_scope):
            return [{"role": "user", "content": "evicted user"}, {"role": "assistant", "content": "evicted answer"}]

        def _midterm_enabled(self):
            return True

    memory = LayeredMemory()
    recorder = trace_script.TraceRecorder(True)
    trace_script.install_test_eviction_trace(memory, recorder)

    with recorder.turn(3):
        evicted = memory._save_short_term_messages([], "scope")

    assert [message["content"] for message in evicted] == ["evicted user", "evicted answer"]
    assert recorder.evictions == [
        {
            "turn": 3,
            "evicted_count": 2,
            "evicted_messages": evicted,
            "midterm_triggered": True,
            "long_term_triggered": True,
        }
    ]


def test_reset_test_databases_initializes_entity_store_before_reset():
    class ResettableMemory:
        def __init__(self):
            self.events = []
            self.records = ["old-memory"]
            self.entity_records = ["old-entity"]

        @property
        def entity_store(self):
            self.events.append("initialize-entity-store")
            return self.entity_records

        def reset(self):
            self.events.append("reset")
            self.records.clear()
            self.entity_records.clear()

    memory = ResettableMemory()

    trace_script.reset_test_databases(memory)

    assert memory.events == ["initialize-entity-store", "reset"]
    assert memory.records == []
    assert memory.entity_records == []


def test_main_resets_database_before_starting_runner(tmp_path, monkeypatch):
    events = []
    memory = SimpleNamespace()
    args = SimpleNamespace(
        data=tmp_path / "dataset.json",
        output_root=tmp_path / "output",
        state_root=tmp_path / "state",
        user_id=None,
        test_mode=False,
        top_k=20,
        threshold=0.1,
        rerank=False,
        continue_on_error=False,
    )
    turns = [{"user_id": "user-1"}]

    monkeypatch.setattr(trace_script, "parse_args", lambda _: args)
    monkeypatch.setattr(
        trace_script,
        "load_selected_turns",
        lambda *_args, **_kwargs: ([{"session_id": "S002"}], turns, "user-1"),
    )
    monkeypatch.setattr(trace_script, "build_memory", lambda _: (events.append("build") or memory, {}))
    monkeypatch.setattr(
        trace_script,
        "reset_test_databases",
        lambda candidate: events.append("reset") if candidate is memory else pytest.fail("wrong memory"),
    )
    monkeypatch.setattr(trace_script, "close_memory", lambda candidate: events.append("close"))

    class FakeRunner:
        def __init__(self, **kwargs):
            assert kwargs["memory"] is memory
            events.append("runner-init")
            self.output_dir = None

        def run(self):
            events.append("run")
            return {"completed_turn_count": 0}

    monkeypatch.setattr(trace_script, "RetrieveContextAddTraceRunner", FakeRunner)

    trace_script.main([])

    assert events == ["build", "reset", "runner-init", "run", "close"]


def test_resolve_llm_identity_supports_mapping_and_wrapped_configs():
    configured_memory = SimpleNamespace(
        config={"llm": {"provider": "deepseek", "config": {"model": "deepseek-v4-flash"}}}
    )
    assert trace_script.resolve_llm_identity(configured_memory, SimpleNamespace()) == (
        "deepseek",
        "deepseek-v4-flash",
    )

    wrapped_llm = SimpleNamespace(config=SimpleNamespace(model_name="wrapped-model"))
    wrapped = SimpleNamespace(_wrapped=wrapped_llm)
    assert trace_script.resolve_llm_identity(SimpleNamespace(config={}), wrapped) == ("unknown", "wrapped-model")


def _comprehensive_snapshot():
    return {
        "timestamp": "2026-07-21T00:00:00+00:00",
        "sqlite": {
            "tables": {
                "messages": [
                    {
                        "id": "m1",
                        "role": "user",
                        "content": "第一行\n第二行 | 管道",
                        "details": {"nested": ["A|B", {"enabled": True}]},
                    }
                ],
                "history": [],
            }
        },
        "long_term_memory": [
            {
                "id": "long-1",
                "payload": {
                    "data": "长期记忆完整文本",
                    "user_id": "user-1",
                    "run_id": "run-1",
                    "role": "user",
                    "created_at": "2026-07-21T01:00:00+00:00",
                    "custom": {"origin": "test|suite"},
                    "embedding": [0.1, 0.2, 0.3],
                },
                "metadata": {"source": "mock"},
            }
        ],
        "midterm_memory": {
            "sessions": [
                {
                    "id": "session-vector-1",
                    "payload": {"session_id": "session-1", "topic": "资产配置", "summary": "会话摘要"},
                }
            ],
            "pages": [
                {
                    "id": "page-vector-1",
                    "payload": {
                        "session_id": "session-1",
                        "page_id": "page-1",
                        "summary": "页面摘要",
                        "messages": [{"role": "user", "content": "消息"}],
                    },
                }
            ],
        },
        "entity_store": {
            "initialized": True,
            "records": [
                {
                    "id": "entity-1",
                    "payload": {
                        "data": "ETF",
                        "entity_type": "product",
                        "linked_memory_ids": ["long-1"],
                        "user_id": "user-1",
                        "run_id": "run-1",
                        "created_at": "2026-07-21T10:00:00+08:00",
                        "updated_at": "2026-07-21T10:05:00+08:00",
                    },
                }
            ],
        },
        "profile_view": {
            "user_id": "user-1",
            "profile": {
                "risk_level": {
                    "value": "conservative",
                    "value_version": 2,
                    "updated_at": "2026-07-21T02:00:00+00:00",
                    "source_type": "explicit",
                },
                "preferred_products": ["ETF"],
            },
        },
        "warnings": ["long-term vector store snapshot is incomplete: mock warning"],
    }


def test_database_snapshot_renders_each_store_as_separate_tables():
    rendered = trace_script.render_database_snapshot(_comprehensive_snapshot())

    assert "## SQLite 数据库" in rendered
    assert "### 表：messages" in rendered
    assert "### 表：history" in rendered
    assert "第一行<br>第二行 \\| 管道" in rendered
    assert '"nested":["A\\|B",{"enabled":true}]' in rendered
    assert "### 表：history\n\n记录数：0\n\n当前表为空。" in rendered
    assert "## 长期记忆向量库" in rendered
    assert "长期记忆完整文本" in rendered
    assert "embedding" not in rendered
    assert "### Mid-term Sessions" in rendered
    assert "### Mid-term Pages" in rendered
    assert "session-1" in rendered and "page-1" in rendered
    assert "## Entity Store" in rendered
    assert "状态：已初始化" in rendered
    assert (
        "| id | data | entity_type | linked_memory_ids | user_id | run_id | created_at | updated_at |" in rendered
    )
    assert "2026-07-21T10:00:00+08:00" in rendered
    assert "2026-07-21T10:05:00+08:00" in rendered
    assert "## 用户画像" in rendered
    assert "conservative" in rendered
    assert "## 数据库快照警告" in rendered
    assert "mock warning" in rendered
    assert '```json\n{\n  "sqlite"' not in rendered


def test_database_snapshot_handles_empty_tables_uninitialized_entity_and_empty_profile():
    snapshot = {
        "timestamp": "2026-07-21T00:00:00+00:00",
        "sqlite": {"tables": {"messages": []}},
        "long_term_memory": [],
        "midterm_memory": {"sessions": [], "pages": []},
        "entity_store": {"initialized": False, "records": []},
        "profile_view": {"risk_level": "conservative"},
        "warnings": [],
    }
    rendered = trace_script.render_database_snapshot(snapshot)

    assert "### 表：messages\n\n记录数：0\n\n当前表为空。" in rendered
    assert "当前长期记忆向量库为空。" in rendered
    assert rendered.count("当前表为空。") >= 3
    assert "Entity Store 尚未初始化。" in rendered
    assert "| risk_level | conservative |" in rendered
    assert "无快照警告。" in rendered

    snapshot["profile_view"] = {"user_id": "user-1", "profile": {}}
    assert "当前用户画像为空。" in trace_script.render_database_snapshot(snapshot)


def test_llm_logs_redact_api_keys():
    recorder = trace_script.TraceRecorder(False)
    wrapped = trace_script.TracedLLM(FakeLLM(), recorder, provider="mock", model="mock-model")

    wrapped.generate_response(
        messages=[{"role": "user", "content": "safe prompt"}],
        api_key="must-not-appear",
    )
    rendered = trace_script._render_llm_calls(recorder.calls)

    assert "must-not-appear" not in rendered
    assert "***REDACTED***" in rendered

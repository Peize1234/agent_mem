import hashlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest

from mem0.configs.base import MemoryConfig
from mem0.memory.storage import SQLiteManager
from memory_monitor.components import chat_panel, common, memory_panel, pipeline_panel, styles
from memory_monitor.config import DemoLabConfig
from memory_monitor.models import PIPELINE_STEPS, PipelineStep, StepStatus
from memory_monitor.runtime import DemoBackgroundCoordinator, DemoMemory
from memory_monitor.services.demo_pipeline_service import DemoPipelineService, STEP_SNAPSHOT_SECTIONS
from memory_monitor.services.demo_repository import (
    DemoRepository,
    StepAlreadyRunningError,
    TurnSessionMismatchError,
)
from memory_monitor.services.memory_state_service import MemoryStateService
from memory_monitor.services.simulation_service import SimulationService
from memory_monitor.views import demo_lab
from memory_monitor.views.demo_lab import resolve_selected_turn_id, synchronize_selected_turn_id

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _FakeStateService:
    def __init__(self, memory):
        self.memory = memory

    def snapshot(self, *, user_id, run_id, sections=None):
        state = deepcopy(self.memory.state)
        if sections is None:
            state["scope"] = {"user_id": user_id, "run_id": run_id}
            return state
        selected = set(sections)
        snapshot = {section: state[section] for section in selected if section in state}
        if selected & {"migration_jobs", "profile_jobs"}:
            snapshot["jobs"] = {}
            if "migration_jobs" in selected:
                snapshot["jobs"]["migration"] = state["jobs"]["migration"]
            if "profile_jobs" in selected:
                snapshot["jobs"]["profile"] = state["jobs"]["profile"]
        return snapshot

    compare = staticmethod(MemoryStateService.compare)


class _FakeWorker:
    def __init__(self, memory):
        self.memory = memory
        self.calls = {"midterm": [], "longterm": [], "profile": []}
        self.jobs = {
            "migration-1": {
                "job_id": "migration-1",
                "status": "pending",
                "midterm_status": "pending",
                "longterm_status": "pending",
            },
            "profile-1": {"job_id": "profile-1", "status": "pending"},
        }

    def process_midterm_job(self, job_id):
        self.calls["midterm"].append(job_id)
        job = self.jobs[job_id]
        if job["midterm_status"] == "succeeded":
            return False
        job["midterm_status"] = "succeeded"
        self._refresh_migration(job)
        self.memory.state["jobs"]["migration"][0].update(deepcopy(job))
        self.memory.state["midterm_pages"].append({"id": "mid-1", "payload": {"data": "summary"}})
        self.memory._events.append({"event_type": "job.finished", "job_id": job_id, "stage": "midterm"})
        return True

    def process_longterm_job(self, job_id):
        self.calls["longterm"].append(job_id)
        job = self.jobs[job_id]
        if job["longterm_status"] == "succeeded":
            return False
        job["longterm_status"] = "succeeded"
        self._refresh_migration(job)
        self.memory.state["jobs"]["migration"][0].update(deepcopy(job))
        self.memory.state["long_term"].append({"id": "long-1", "payload": {"data": "durable"}})
        self.memory._events.append({"event_type": "job.finished", "job_id": job_id, "stage": "longterm"})
        return True

    def process_profile_job(self, job_id):
        self.calls["profile"].append(job_id)
        job = self.jobs[job_id]
        if job["status"] == "succeeded":
            return False
        job["status"] = "succeeded"
        self.memory.state["jobs"]["profile"][0]["status"] = "succeeded"
        self.memory.state["profile"].append({"attribute_id": 1, "attribute_key": "risk_level", "value": "balanced"})
        self.memory._events.append({"event_type": "job.finished", "job_id": job_id})
        return True

    def get_job_status(self, job_id, job_type):
        return deepcopy(self.jobs[job_id])

    @staticmethod
    def _refresh_migration(job):
        job["status"] = "succeeded" if {job["midterm_status"], job["longterm_status"]} == {"succeeded"} else "pending"


class _FakeDemoMemory:
    def __init__(self, *, commit_failures=0):
        self.retrieve_calls = 0
        self.build_calls = 0
        self.generation_calls = 0
        self.commit_calls = 0
        self.commit_kwargs = []
        self.commit_failures = commit_failures
        self.generated_messages = None
        self._events = []
        self.state = {
            "short_term": [],
            "midterm_sessions": [],
            "midterm_pages": [],
            "long_term": [],
            "profile": [],
            "jobs": {"migration": [], "profile": []},
        }
        self.demo_background_worker = _FakeWorker(self)

    def retrieve_context_for_demo(self, query, *, user_id, session_id):
        self.retrieve_calls += 1
        context = {
            "query": query,
            "user_id": user_id,
            "session_id": session_id,
            "profile": {},
            "short_term_messages": [],
            "retrieved_memories": [],
        }
        context["context_hash"] = self.context_hash(context)
        return context

    def build_prompt_from_context(self, context):
        self.build_calls += 1
        return [{"role": "system", "content": f"frozen:{context['query']}"}]

    def generate_response_for_demo(self, messages, **kwargs):
        self.generation_calls += 1
        self.generated_messages = deepcopy(messages)
        return "model answer"

    def commit_demo_turn(self, **kwargs):
        self.commit_calls += 1
        self.commit_kwargs.append(deepcopy(kwargs))
        if self.commit_failures:
            self.commit_failures -= 1
            raise RuntimeError("temporary commit failure")
        self.state["short_term"].extend(
            [
                {"id": "message-user", "role": "user", "content": kwargs["user_message"]},
                {"id": "message-assistant", "role": "assistant", "content": kwargs["assistant_message"]},
            ]
        )
        self.state["jobs"]["migration"] = [{"job_id": "migration-1", "status": "pending"}]
        self.state["jobs"]["profile"] = [{"job_id": "profile-1", "status": "pending"}]
        return {
            "results": [],
            "background": {
                "migration_job_id": "migration-1",
                "profile_job_id": "profile-1",
            },
        }

    @staticmethod
    def context_hash(context):
        payload = deepcopy(context)
        payload.pop("context_hash", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def demo_events(self):
        return deepcopy(self._events)


class _PersistentlyIdempotentFakeMemory(_FakeDemoMemory):
    def __init__(self, persisted_commits, shared_state=None):
        super().__init__()
        self.persisted_commits = persisted_commits
        if shared_state is not None:
            self.state = shared_state
            self.demo_background_worker = _FakeWorker(self)

    def commit_demo_turn(self, **kwargs):
        self.commit_calls += 1
        self.commit_kwargs.append(deepcopy(kwargs))
        key = (kwargs["simulation_id"], kwargs["turn_id"])
        request = {field: kwargs[field] for field in ("user_id", "run_id", "user_message", "assistant_message")}
        existing = self.persisted_commits.get(key)
        if existing is not None:
            if existing["request"] != request:
                raise ValueError("idempotency conflict")
            return deepcopy(existing["result"])

        self.state["short_term"].extend(
            [
                {"id": "message-user", "role": "user", "content": kwargs["user_message"]},
                {"id": "message-assistant", "role": "assistant", "content": kwargs["assistant_message"]},
            ]
        )
        self.state["jobs"]["migration"] = [{"job_id": "migration-1", "status": "pending"}]
        self.state["jobs"]["profile"] = [{"job_id": "profile-1", "status": "pending"}]
        result = {
            "results": [],
            "background": {
                "migration_job_id": "migration-1",
                "profile_job_id": "profile-1",
            },
        }
        self.persisted_commits[key] = {"request": request, "result": deepcopy(result)}
        return result


def _pipeline(tmp_path, *, memory=None):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("simulation-1", "user-1", "run-1")
    memory = memory or _FakeDemoMemory()
    pipeline = DemoPipelineService(memory, repository, _FakeStateService(memory))
    turn = pipeline.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="What changed?",
    )
    return pipeline, repository, memory, session, turn


def _coordinator(pipeline, repository, name="test"):
    coordinator = DemoBackgroundCoordinator(name, repository)
    pipeline.coordinator = coordinator
    return coordinator


def test_turn_selection_clears_stale_id_for_empty_session_and_uses_latest_for_existing_session():
    turns_a = [{"turn_id": "turn-a"}]
    turns_b = [{"turn_id": "turn-b-1"}, {"turn_id": "turn-b-2"}]

    assert resolve_selected_turn_id(turns_a, "turn-a") == "turn-a"
    assert resolve_selected_turn_id([], "turn-a") is None
    assert resolve_selected_turn_id(turns_b, "turn-a") == "turn-b-2"

    state = {"demo_turn_id": "turn-a", "unrelated": "preserved"}
    assert synchronize_selected_turn_id(state, []) is None
    assert state == {"unrelated": "preserved"}
    assert synchronize_selected_turn_id(state, turns_b) == "turn-b-2"
    assert state["demo_turn_id"] == "turn-b-2"


def test_chat_history_uses_tall_keyed_native_scroll_container():
    containers = []

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Streamlit:
        @staticmethod
        def subheader(value):
            return None

        @staticmethod
        def container(*, height, border, key, autoscroll):
            containers.append(
                {
                    "height": height,
                    "border": border,
                    "key": key,
                    "autoscroll": autoscroll,
                }
            )
            return _Context()

        @staticmethod
        def caption(value):
            return None

    chat_panel.render_history(
        _Streamlit(),
        [],
        simulation_id="simulation-1",
        session_id="session-1",
    )

    assert chat_panel.CHAT_HISTORY_HEIGHT == 700
    assert containers == [
        {
            "height": 700,
            "border": True,
            "key": "chat_history_simulation-1_session-1",
            "autoscroll": False,
        }
    ]
    chat_style = styles._DEMO_LAB_CSS.split('div[class*="st-key-chat_history_"] {', 1)[1].split("}", 1)[0]
    assert "overscroll-behavior-y: contain" in chat_style
    assert "scrollbar-gutter: stable" in chat_style


def test_chat_history_fragment_reloads_messages_and_renders_new_assistant_reply():
    class _Repository:
        calls = []

        @classmethod
        def raw_messages(cls, session_id):
            cls.calls.append(session_id)
            messages = [{"role": "user", "content": "question"}]
            if len(cls.calls) > 1:
                messages.append({"role": "assistant", "content": "new answer"})
            return messages

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Streamlit:
        fragment_callback = None
        fragment_intervals = []
        rendered = []
        container_keys = []

        @classmethod
        def fragment(cls, *, run_every):
            cls.fragment_intervals.append(run_every)

            def decorate(callback):
                cls.fragment_callback = callback
                return callback

            return decorate

        @staticmethod
        def subheader(value):
            return None

        @classmethod
        def container(cls, *, height, border, key, autoscroll):
            cls.container_keys.append(key)
            return _Context()

        @staticmethod
        def chat_message(role):
            return _Context()

        @classmethod
        def markdown(cls, value):
            cls.rendered.append(value)

        @staticmethod
        def caption(value):
            return None

    demo_lab._render_chat_history_workspace(
        _Streamlit(),
        _Repository(),
        simulation_id="simulation-1",
        session_id="session-1",
        poll_interval_seconds=0.4,
    )
    assert _Repository.calls == ["session-1"]
    assert "new answer" not in _Streamlit.rendered

    _Streamlit.fragment_callback()

    assert _Repository.calls == ["session-1", "session-1"]
    assert _Streamlit.rendered[-1] == "new answer"
    assert _Streamlit.fragment_intervals == [0.4]
    assert set(_Streamlit.container_keys) == {"chat_history_simulation-1_session-1"}


def test_right_controls_and_live_status_share_one_fragment_boundary():
    page_source = inspect.getsource(demo_lab.render)
    chat_fragment_source = inspect.getsource(demo_lab._render_chat_history_workspace)
    fragment_source = inspect.getsource(demo_lab._render_right_workspace)
    content_source = inspect.getsource(demo_lab._render_right_workspace_content)

    assert "_render_chat_history_workspace" in page_source
    assert "chat_panel.chat_input" in page_source
    assert page_source.index("_render_chat_history_workspace") < page_source.index("chat_panel.chat_input")
    assert page_source.index("chat_panel.chat_input") < page_source.index("_render_right_workspace")
    assert "repository.raw_messages" not in page_source
    assert "pipeline_panel.render_controls" not in page_source
    assert "pipeline_panel.apply_action" not in page_source
    assert "pipeline_panel.render_memory_gates" not in page_source

    assert "@st.fragment(run_every=poll_interval_seconds)" in chat_fragment_source
    assert "repository.raw_messages(session_id)" in chat_fragment_source
    assert "chat_panel.render_history" in chat_fragment_source
    assert "session_id=session_id" in chat_fragment_source
    assert "@st.fragment(run_every=poll_interval_seconds)" in fragment_source
    assert 'st.rerun(scope="app")' not in fragment_source
    assert "logger.exception" in fragment_source
    assert "重新加载右侧区域" in fragment_source
    assert fragment_source.count("repository.list_turns(session_id)") == 1
    assert fragment_source.count("repository.list_steps(turn_id)") == 1
    assert "pipeline_panel.render_memory_gates" in content_source
    assert "pipeline_panel.render_controls" in content_source
    assert "pipeline_panel.apply_action" in content_source
    assert "_render_turn_navigation" in content_source
    assert "st.segmented_control" in content_source
    assert "st.tabs" not in content_source
    assert "st.container(height=450" in content_source
    assert "repository.list_steps" not in content_source
    assert "_render_live_workspace" not in inspect.getsource(demo_lab)


def test_restore_existing_environment_is_read_only_and_never_creates_session(tmp_path):
    repository = DemoRepository(tmp_path / "restore.db")
    session = repository.create_session("restore", "user-1", "run-1")
    original_updated_at = session["updated_at"]
    environment = SimpleNamespace(repository=repository)

    class _Service:
        create_session_calls = 0

        @staticmethod
        def environment(simulation_id):
            assert simulation_id == "restore"
            return environment

        @classmethod
        def create_session(cls, *args, **kwargs):
            cls.create_session_calls += 1
            raise AssertionError("page restoration must not create a session")

    class _Streamlit:
        session_state = {
            "demo_simulation_id": "restore",
            "demo_user_id": "user-1",
            "demo_run_id": "run-1",
            "demo_session_id": session["session_id"],
        }

        @staticmethod
        def warning(value):
            raise AssertionError(value)

        @staticmethod
        def error(value):
            raise AssertionError(value)

    first_environment, first_session = demo_lab._restore_environment(_Streamlit(), _Service())
    second_environment, second_session = demo_lab._restore_environment(_Streamlit(), _Service())

    assert first_environment is second_environment is environment
    assert first_session["session_id"] == second_session["session_id"] == session["session_id"]
    assert repository.get_session(session["session_id"])["updated_at"] == original_updated_at
    assert _Service.create_session_calls == 0


def test_right_workspace_renders_only_selected_section(monkeypatch):
    rendered = []

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Streamlit:
        session_state = {}

        @staticmethod
        def container(**kwargs):
            return _Context()

        @staticmethod
        def segmented_control(label, options, **kwargs):
            assert kwargs["key"] == "workspace_section:simulation-1:session-1:turn-1"
            return "Trace"

    monkeypatch.setattr(pipeline_panel, "render_memory_gates", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline_panel, "render_controls", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(demo_lab, "_render_turn_navigation", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline_panel, "render_steps", lambda *args, **kwargs: rendered.append("pipeline"))
    monkeypatch.setattr(demo_lab.context_panel, "render", lambda *args, **kwargs: rendered.append("context"))
    monkeypatch.setattr(demo_lab.prompt_panel, "render_prompt", lambda *args, **kwargs: rendered.append("prompt"))
    monkeypatch.setattr(
        demo_lab.prompt_panel,
        "render_generation",
        lambda *args, **kwargs: rendered.append("generation"),
    )
    monkeypatch.setattr(demo_lab.memory_panel, "render", lambda *args, **kwargs: rendered.append("database"))
    monkeypatch.setattr(demo_lab.trace_panel, "render", lambda *args, **kwargs: rendered.append("trace"))
    monkeypatch.setattr(
        demo_lab,
        "_latest_session_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("hidden database page rendered")),
    )

    demo_lab._render_right_workspace_content(
        _Streamlit(),
        SimpleNamespace(simulation_id="simulation-1", repository=object(), pipeline=object()),
        "session-1",
        {"turn_id": "turn-1"},
        [],
        [],
        [],
    )

    assert rendered == ["trace"]


def test_chat_fragment_degrades_sqlite_lock_to_local_warning():
    warnings = []

    class _Repository:
        @staticmethod
        def raw_messages(session_id):
            raise sqlite3.OperationalError("database is locked")

    class _Streamlit:
        @staticmethod
        def fragment(*, run_every):
            return lambda callback: callback

        @staticmethod
        def warning(value):
            warnings.append(value)

    demo_lab._render_chat_history_workspace(
        _Streamlit(),
        _Repository(),
        simulation_id="simulation-1",
        session_id="session-1",
        poll_interval_seconds=1.0,
    )

    assert warnings == ["对话数据库正忙，将在下一次刷新时自动重试。"]


def test_default_polling_is_stable_and_environment_override_is_bounded(monkeypatch):
    assert DemoLabConfig().poll_interval_seconds == 1.0
    monkeypatch.setenv("MEMORY_MONITOR_POLL_INTERVAL_SECONDS", "0.2")
    assert DemoLabConfig.from_env().poll_interval_seconds == 0.5
    monkeypatch.setenv("MEMORY_MONITOR_POLL_INTERVAL_SECONDS", "0.8")
    assert DemoLabConfig.from_env().poll_interval_seconds == 0.8


def test_page_has_one_memory_gate_group_bound_to_selected_turn_step_holds(tmp_path):
    page_source = inspect.getsource(demo_lab)
    panel_source = inspect.getsource(pipeline_panel)
    assert page_source.count(".toggle(") == 0
    assert panel_source.count(".toggle(") == 1
    assert "记忆步骤操作" not in page_source + panel_source
    assert "render_memory_config" not in page_source + panel_source
    assert "本轮记忆步骤开关" in panel_source

    pipeline, repository, _, session, first = _pipeline(tmp_path)
    pipeline.hold_step(
        first["turn_id"],
        PipelineStep.RUN_LONGTERM,
        session_id=session["session_id"],
    )
    second = pipeline.create_turn(
        session["session_id"],
        user_id=session["user_id"],
        run_id=session["run_id"],
        user_message="second config",
    )
    pipeline.hold_step(second["turn_id"], PipelineStep.RUN_MIDTERM, session_id=session["session_id"])
    pipeline.hold_step(second["turn_id"], PipelineStep.RUN_PROFILE, session_id=session["session_id"])
    toggles = []

    class _Column:
        def __init__(self, st):
            self.st = st

        def toggle(self, label, *, key, disabled, help, on_change, args):
            if key in self.st.changes:
                self.st.session_state[key] = self.st.changes.pop(key)
                on_change(*args)
            toggles.append((key, self.st.session_state[key], disabled))
            return self.st.session_state[key]

    class _Streamlit:
        session_state = {}
        captions = []
        changes = {}

        @classmethod
        def caption(cls, value):
            cls.captions.append(value)

        @classmethod
        def columns(cls, count):
            return [_Column(cls) for _ in range(count)]

    pipeline_panel.render_memory_gates(
        _Streamlit(),
        pipeline,
        repository,
        simulation_id="simulation-1",
        session_id=session["session_id"],
        turn_id=None,
    )
    assert toggles == []

    pipeline_panel.render_memory_gates(
        _Streamlit(),
        pipeline,
        repository,
        simulation_id="simulation-1",
        session_id=session["session_id"],
        turn_id=first["turn_id"],
    )
    assert [value for _key, value, _disabled in toggles[-4:]] == [True, True, False, True]
    assert all(not disabled for _key, _value, disabled in toggles[-4:])
    assert all(
        key.startswith(f"memory_gate:simulation-1:{first['turn_id']}:") for key, _value, _disabled in toggles[-4:]
    )

    gate_calls = []
    set_memory_step_runnable = pipeline.set_memory_step_runnable

    def tracked_gate(*args, **kwargs):
        gate_calls.append((args, kwargs))
        return set_memory_step_runnable(*args, **kwargs)

    pipeline.set_memory_step_runnable = tracked_gate
    longterm_key = f"memory_gate:simulation-1:{first['turn_id']}:run_longterm"
    _Streamlit.changes[longterm_key] = True
    pipeline_panel.render_memory_gates(
        _Streamlit(),
        pipeline,
        repository,
        simulation_id="simulation-1",
        session_id=session["session_id"],
        turn_id=first["turn_id"],
    )
    released = repository.get_step(first["turn_id"], PipelineStep.RUN_LONGTERM)
    assert released["status"] == "pending"
    assert released["is_held"] is False
    assert len(gate_calls) == 1

    pipeline_panel.render_memory_gates(
        _Streamlit(),
        pipeline,
        repository,
        simulation_id="simulation-1",
        session_id=session["session_id"],
        turn_id=second["turn_id"],
    )
    assert [value for _key, value, _disabled in toggles[-4:]] == [True, False, True, False]

    repository.mark_background_submitted(second["turn_id"])
    pipeline_panel.render_memory_gates(
        _Streamlit(),
        pipeline,
        repository,
        simulation_id="simulation-1",
        session_id=session["session_id"],
        turn_id=second["turn_id"],
    )
    assert all(not disabled for _key, _value, disabled in toggles[-4:])
    assert "本轮记忆任务已提交，配置不可修改。" not in _Streamlit.captions


def test_apply_action_has_no_success_toast_or_page_rerun():
    class _Pipeline:
        @staticmethod
        def run_next_step(turn_id, *, session_id):
            return {"turn_id": turn_id, "submissions": {}}

    class _Streamlit:
        captions = []

        @classmethod
        def caption(cls, value):
            cls.captions.append(value)

        @staticmethod
        def toast(value):
            raise AssertionError("normal actions must not use a toast")

        @staticmethod
        def rerun(*, scope):
            raise AssertionError(f"normal actions must not rerun the app: {scope}")

        @staticmethod
        def warning(value):
            raise AssertionError(value)

        @staticmethod
        def error(value):
            raise AssertionError(value)

    pipeline_panel.apply_action(
        _Streamlit(),
        _Pipeline(),
        object(),
        "turn-1",
        "session-1",
        "next",
    )

    source = inspect.getsource(pipeline_panel.apply_action)
    assert ".toast(" not in source
    assert ".rerun(" not in source
    assert _Streamlit.captions == []


def test_memory_gate_toggle_holds_only_its_step_and_never_creates_skipped(tmp_path):
    pipeline, repository, _, session, turn = _pipeline(tmp_path)
    prefix = f"memory_gate:simulation-1:{turn['turn_id']}"
    state = {f"{prefix}:{step.value}": True for step in PipelineStep if step.value.startswith("run_")}

    state[f"{prefix}:{PipelineStep.RUN_SHORTTERM.value}"] = False
    pipeline_panel._apply_memory_gate(
        state,
        pipeline,
        repository,
        "simulation-1",
        session["session_id"],
        turn["turn_id"],
        "run_shortterm",
    )
    blocked = repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)
    assert blocked["status"] == "pending"
    assert blocked["is_held"] is True
    assert all(step["status"] == "pending" for step in repository.list_steps(turn["turn_id"])[4:])
    assert all(not step["is_held"] for step in repository.list_steps(turn["turn_id"])[5:])

    state[f"{prefix}:{PipelineStep.RUN_SHORTTERM.value}"] = True
    pipeline_panel._apply_memory_gate(
        state,
        pipeline,
        repository,
        "simulation-1",
        session["session_id"],
        turn["turn_id"],
        "run_shortterm",
    )
    released = repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)
    assert released["status"] == "pending"
    assert released["is_held"] is False
    assert repository.get_turn(turn["turn_id"])["execution_target"] is None
    assert repository.get_turn(turn["turn_id"])["background_submitted_at"] is None
    assert state[f"{prefix}:gate_notice"] == "已开启，等待点击“下一步”或其他运行按钮。"

    callback_source = inspect.getsource(pipeline_panel._apply_memory_gate)
    assert "set_memory_step_runnable" in callback_source
    assert "pipeline.release_step" not in callback_source
    assert "pipeline.hold_step" not in callback_source


def test_release_step_only_clears_held_flag_even_when_dependencies_are_complete(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "release-only")
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        pipeline.hold_step(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            session_id=session["session_id"],
        )

        released = pipeline.release_step(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            session_id=session["session_id"],
        )

        assert released["status"] == StepStatus.PENDING.value
        assert released["is_held"] is False
        assert repository.get_turn(turn["turn_id"])["execution_target"] is None
        assert repository.get_turn(turn["turn_id"])["background_submitted_at"] is None
        assert coordinator.wait_for_idle(0.2)
        assert memory.commit_calls == 0
    finally:
        coordinator.shutdown(wait=True)


def test_enabling_gate_without_execution_target_only_releases_step(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "gate-without-target")
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        pipeline.hold_step(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            session_id=session["session_id"],
        )

        result = pipeline.set_memory_step_runnable(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            True,
            session_id=session["session_id"],
        )

        assert result["changed"] is True
        assert result["resumed"] is False
        assert result["submissions"] == {}
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)["status"] == "pending"
        assert repository.get_turn(turn["turn_id"])["execution_target"] is None
        assert repository.get_turn(turn["turn_id"])["background_submitted_at"] is None
        assert memory.commit_calls == 0
    finally:
        coordinator.shutdown(wait=True)


def test_enabling_gate_with_answer_target_does_not_start_memory(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "gate-answer-target")
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        pipeline.hold_step(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            session_id=session["session_id"],
        )
        repository.set_execution_target(turn["turn_id"], "answer")

        result = pipeline.set_memory_step_runnable(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            True,
            session_id=session["session_id"],
        )

        assert result["changed"] is True
        assert result["resumed"] is False
        assert result["submissions"] == {}
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)["status"] == "pending"
        assert repository.get_turn(turn["turn_id"])["execution_target"] == "answer"
        assert repository.get_turn(turn["turn_id"])["background_submitted_at"] is None
        assert memory.commit_calls == 0
    finally:
        coordinator.shutdown(wait=True)


def test_enabling_gate_resumes_started_memory_target(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "gate-memory-target")
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        pipeline.hold_step(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            session_id=session["session_id"],
        )
        stalled = pipeline.run_memory_stage(turn["turn_id"], session_id=session["session_id"])
        assert stalled["submissions"] == {}
        assert repository.get_turn(turn["turn_id"])["execution_target"] == "memory"

        result = pipeline.set_memory_step_runnable(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            True,
            session_id=session["session_id"],
        )

        assert result["changed"] is True
        assert result["resumed"] is True
        assert list(result["submissions"]) == [PipelineStep.RUN_SHORTTERM]
        assert coordinator.wait_for_idle(3)
        assert [step["status"] for step in repository.list_steps(turn["turn_id"])[4:]] == ["succeeded"] * 4
        assert memory.commit_calls == 1
    finally:
        coordinator.shutdown(wait=True)


def test_setting_pending_memory_gate_never_changes_execution_intent_or_other_steps(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    before = repository.list_steps(turn["turn_id"])

    held = pipeline.set_memory_step_runnable(
        turn["turn_id"],
        PipelineStep.RUN_LONGTERM,
        False,
        session_id=session["session_id"],
    )

    assert held["changed"] is True
    assert held["resumed"] is False
    assert held["submissions"] == {}
    after_hold = repository.list_steps(turn["turn_id"])
    assert after_hold[6]["status"] == "pending"
    assert after_hold[6]["is_held"] is True
    assert [(step["step"], step["status"], step["is_held"]) for index, step in enumerate(after_hold) if index != 6] == [
        (step["step"], step["status"], step["is_held"]) for index, step in enumerate(before) if index != 6
    ]
    assert repository.get_turn(turn["turn_id"])["execution_target"] is None
    assert repository.get_turn(turn["turn_id"])["background_submitted_at"] is None
    assert memory.commit_calls == 0

    released = pipeline.set_memory_step_runnable(
        turn["turn_id"],
        PipelineStep.RUN_LONGTERM,
        True,
        session_id=session["session_id"],
    )
    assert released["changed"] is True
    assert released["resumed"] is False
    assert repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)["status"] == "pending"
    assert repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)["is_held"] is False
    assert repository.get_turn(turn["turn_id"])["execution_target"] is None
    assert repository.get_turn(turn["turn_id"])["background_submitted_at"] is None


def test_rechecking_held_gate_resumes_existing_run_all_without_another_action(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "gate-resume")
    pipeline.hold_step(turn["turn_id"], PipelineStep.RUN_MIDTERM, session_id=session["session_id"])
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["is_held"] is True
        assert memory.demo_background_worker.calls["midterm"] == []

        prefix = f"memory_gate:simulation-1:{turn['turn_id']}"
        state = {f"{prefix}:{PipelineStep.RUN_MIDTERM.value}": True}
        pipeline_panel._apply_memory_gate(
            state,
            pipeline,
            repository,
            "simulation-1",
            session["session_id"],
            turn["turn_id"],
            PipelineStep.RUN_MIDTERM.value,
        )
        assert coordinator.wait_for_idle(3)

        resumed = repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)
        assert resumed["status"] == "succeeded"
        assert resumed["is_held"] is False
        assert memory.demo_background_worker.calls["midterm"] == ["migration-1"]
        assert memory.commit_calls == 1
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)["attempts"] == 1
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)["attempts"] == 1
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_PROFILE)["attempts"] == 1
    finally:
        coordinator.shutdown(wait=True)


def test_gate_cannot_hold_queued_running_succeeded_or_failed_steps(tmp_path):
    pipeline, repository, _, session, _turn_row = _pipeline(tmp_path)
    statuses = {}
    for target_status in ("queued", "running", "succeeded", "failed"):
        turn = pipeline.create_turn(
            session["session_id"],
            user_id=session["user_id"],
            run_id=session["run_id"],
            user_message=f"status {target_status}",
        )
        token = repository.queue_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)
        if target_status != "queued":
            assert repository.start_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM, token)
        if target_status == "succeeded":
            repository.complete_step(
                turn["turn_id"],
                PipelineStep.RUN_SHORTTERM,
                token,
                input_data={},
                output_data={},
                duration_ms=0,
                before_snapshot_id=None,
                after_snapshot_id=None,
                diff={},
            )
        elif target_status == "failed":
            repository.fail_step(
                turn["turn_id"],
                PipelineStep.RUN_SHORTTERM,
                token,
                input_data={},
                error=RuntimeError("expected"),
                duration_ms=0,
                before_snapshot_id=None,
                after_snapshot_id=None,
                diff={},
            )

        prefix = f"memory_gate:simulation-1:{turn['turn_id']}"
        state = {f"{prefix}:{PipelineStep.RUN_SHORTTERM.value}": False}
        pipeline_panel._apply_memory_gate(
            state,
            pipeline,
            repository,
            "simulation-1",
            session["session_id"],
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM.value,
        )
        stored = repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)
        statuses[target_status] = stored["status"]
        assert stored["is_held"] is False
        assert state[f"{prefix}:{PipelineStep.RUN_SHORTTERM.value}"] is True

    assert statuses == {
        "queued": "queued",
        "running": "running",
        "succeeded": "succeeded",
        "failed": "failed",
    }


def test_active_turn_buttons_switch_selection_with_stable_scoped_keys():
    button_keys = []

    class _Column:
        def button(self, label, *, key, type, help, width):
            button_keys.append(key)
            return key.endswith(":turn-old")

    class _Streamlit:
        session_state = {"demo_turn_id": "turn-new"}
        reruns = []

        @staticmethod
        def caption(value):
            return None

        @staticmethod
        def columns(count):
            return [_Column() for _ in range(count)]

        @classmethod
        def rerun(cls, *, scope):
            cls.reruns.append(scope)

    active = [
        {
            "turn_id": "turn-old",
            "user_message": "old unfinished question",
            "created_at": None,
            "status": "failed",
            "completed_steps": 3,
            "total_steps": 8,
            "has_failure": True,
        },
        {
            "turn_id": "turn-new",
            "user_message": "new unfinished question",
            "created_at": None,
            "status": "running",
            "completed_steps": 2,
            "total_steps": 8,
            "has_failure": False,
        },
    ]

    demo_lab._render_active_turns(
        _Streamlit(),
        active,
        "turn-new",
        simulation_id="simulation-1",
    )

    assert button_keys == [
        "active_turn:simulation-1:turn-old",
        "active_turn:simulation-1:turn-new",
    ]
    assert _Streamlit.session_state["demo_turn_id"] == "turn-old"
    assert _Streamlit.reruns == ["fragment"]


def test_active_and_completed_turn_queries_do_not_overlap(tmp_path):
    repository = DemoRepository(tmp_path / "navigation.db")
    session = repository.create_session("navigation", "user-1", "run-1")
    active = repository.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="active",
    )
    completed = repository.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="completed",
    )
    with repository._connection() as connection:
        connection.execute(
            "UPDATE demo_turns SET completed_at = ? WHERE turn_id = ?",
            ("2026-01-01T00:00:00+00:00", completed["turn_id"]),
        )
        connection.commit()

    active_ids = {turn["turn_id"] for turn in repository.list_active_turns(session["session_id"])}
    history_ids = {turn["turn_id"] for turn in repository.list_completed_turns(session["session_id"])}
    assert active_ids == {active["turn_id"]}
    assert history_ids == {completed["turn_id"]}
    assert active_ids.isdisjoint(history_ids)


def test_pipeline_controls_are_disabled_without_a_selected_turn():
    calls = []

    class _Column:
        def button(self, label, *, key, disabled, width):
            calls.append((label, key, disabled, width))
            return False

    class _Streamlit:
        @staticmethod
        def columns(count):
            return [_Column() for _ in range(count)]

    assert pipeline_panel.render_controls(_Streamlit(), key_prefix="simulation:turn", disabled=True) == (None, None)
    assert calls
    assert len({key for _, key, _, _ in calls}) == len(calls)
    assert all(disabled for _, _, disabled, _ in calls)


def test_pipeline_controls_do_not_render_held_step_ui():
    class _Column:
        @staticmethod
        def button(label, *, key, disabled, width):
            return False

    class _Streamlit:
        @staticmethod
        def columns(count):
            return [_Column() for _ in range(count)]

        @staticmethod
        def selectbox(label, options, *, format_func, key):
            return options[0]

    steps = [
        {
            "step": PipelineStep.RUN_SHORTTERM.value,
            "status": StepStatus.PENDING.value,
            "is_held": False,
        },
        {
            "step": PipelineStep.RUN_MIDTERM.value,
            "status": StepStatus.PENDING.value,
            "is_held": True,
        },
    ]
    assert pipeline_panel.render_controls(
        _Streamlit(),
        key_prefix="simulation:turn",
        disabled=False,
        steps=steps,
    ) == (None, None)
    source = inspect.getsource(pipeline_panel)
    assert "memory_step_menu" not in source
    assert "暂缓尚未执行的记忆步骤" not in source
    assert 'action == "hold"' not in source
    assert 'action == "release"' not in source


def test_record_detail_widgets_use_caller_scoped_stable_keys():
    selectbox_keys = []
    expander_keys = []

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Streamlit:
        @staticmethod
        def caption(value):
            return None

        @staticmethod
        def dataframe(value, **kwargs):
            return None

        @staticmethod
        def code(value, **kwargs):
            return None

        @staticmethod
        def selectbox(label, options, *, format_func, key):
            selectbox_keys.append(key)
            return options[0]

        @staticmethod
        def expander(label, *, expanded, key):
            expander_keys.append(key)
            return _Context()

    records = [{"id": "record-1", "value": "same"}]
    common.render_records(_Streamlit(), records, key_prefix="simulation:turn:short_term")
    common.render_records(_Streamlit(), records, key_prefix="simulation:turn:long_term")

    assert selectbox_keys == [
        "simulation:turn:short_term:detail_selector",
        "simulation:turn:long_term:detail_selector",
    ]
    assert len(set(selectbox_keys + expander_keys)) == 4


def test_table_normalization_handles_mixed_nested_and_scalar_values_without_mutation():
    created = datetime(2026, 7, 30, 12, 34, tzinfo=timezone.utc)
    records = [
        {
            "属性": "preferences",
            "值": ["稳健", {"周期": "长期"}],
            "tuple": ("a", 1),
            "set": {"b", "a"},
            "created_at": created,
            "payload": b"\xe4\xb8\xad\xe6\x96\x87",
            "nullable": None,
        },
        {
            "属性": "risk",
            "值": "低",
            "tuple": "scalar",
            "set": 3,
            "created_at": None,
            "payload": "text",
            "nullable": None,
        },
    ]
    original = deepcopy(records)

    normalized = common.normalize_table_rows(records)

    assert records == original
    assert all(isinstance(row["值"], str) for row in normalized)
    assert json.loads(normalized[0]["值"]) == ["稳健", {"周期": "长期"}]
    assert json.loads(normalized[0]["tuple"]) == ["a", 1]
    assert json.loads(normalized[0]["set"]) == ["a", "b"]
    assert normalized[0]["created_at"] == created.isoformat()
    assert normalized[0]["payload"] == "中文"
    assert normalized[0]["nullable"] is None
    assert normalized[1]["set"] == "3"
    assert pa.Table.from_pylist(normalized).num_rows == 2


def test_render_records_uses_normalized_table_but_keeps_original_detail_json():
    tables = []
    details = []

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Streamlit:
        @staticmethod
        def caption(value):
            return None

        @staticmethod
        def dataframe(value, **kwargs):
            tables.append(value)

        @staticmethod
        def selectbox(label, options, *, format_func, key):
            return options[0]

        @staticmethod
        def expander(label, *, expanded, key):
            return _Context()

        @staticmethod
        def code(value, **kwargs):
            details.append(json.loads(value))

    records = [{"id": "nested", "value": [1, {"key": "value"}]}]
    common.render_records(_Streamlit(), records, key_prefix="records:test")

    assert isinstance(tables[0][0]["value"], str)
    assert details[-1]["value"] == [1, {"key": "value"}]
    assert records[0]["value"] == [1, {"key": "value"}]


def test_table_render_failure_degrades_to_original_json():
    errors = []
    details = []

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Streamlit:
        @staticmethod
        def dataframe(value, **kwargs):
            raise RuntimeError("arrow unavailable")

        @staticmethod
        def error(value):
            errors.append(value)

        @staticmethod
        def expander(label, *, expanded, key):
            return _Context()

        @staticmethod
        def code(value, **kwargs):
            details.append(json.loads(value))

    records = [{"value": ["still", "structured"]}]
    assert common.render_table(_Streamlit(), records, key_prefix="fallback:test") is False
    assert errors == ["表格暂时无法显示，已切换为原始 JSON。"]
    assert details == [records]


def test_memory_panel_renders_only_selected_database_partition():
    tables = []

    class _Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _Streamlit:
        @staticmethod
        def segmented_control(label, options, *, default, key, label_visibility):
            assert key == "details:simulation:session:turn:records:section"
            return "用户画像"

        @staticmethod
        def caption(value):
            return None

        @staticmethod
        def dataframe(value, **kwargs):
            tables.append(value)

        @staticmethod
        def selectbox(label, options, *, format_func, key):
            return options[0]

        @staticmethod
        def expander(label, *, expanded, key):
            return _Context()

        @staticmethod
        def code(value, **kwargs):
            return None

    snapshot = {
        "short_term": [{"id": "short"}],
        "midterm_sessions": [{"id": "session"}],
        "midterm_pages": [{"id": "page"}],
        "long_term": [{"id": "long"}],
        "profile": [{"id": "profile"}],
        "jobs": {"migration": [{"id": "migration"}], "profile": [{"id": "profile-job"}]},
    }
    memory_panel.render(
        _Streamlit(),
        snapshot,
        key_prefix="details:simulation:session:turn:records",
    )

    assert tables == [[{"id": "profile"}]]
    assert "st.tabs" not in inspect.getsource(memory_panel.render)


def test_monitor_disables_telemetry_before_mem0_import_and_rejects_user_site_dependencies():
    app_path = _REPOSITORY_ROOT / "memory_monitor" / "app.py"
    launcher_path = _REPOSITORY_ROOT / "memory_monitor" / "start_demo_lab.sh"
    app_source = app_path.read_text(encoding="utf-8")
    launcher = launcher_path.read_text(encoding="utf-8")

    assert app_source.index('os.environ.setdefault("MEM0_TELEMETRY", "false")') < app_source.index(
        "from mem0.configs.base import MemoryConfig"
    )
    telemetry_export = 'export MEM0_TELEMETRY="${MEM0_TELEMETRY:-false}"'
    user_site_export = 'export PYTHONNOUSERSITE="${PYTHONNOUSERSITE:-1}"'
    assert launcher.index(telemetry_export) < launcher.index('"${python_command[@]}" -c')
    assert launcher.index(user_site_export) < launcher.index('"${python_command[@]}" -c')
    for module in ("streamlit", "pyarrow", "pandas", "requests", "urllib3", "posthog"):
        assert f'"{module}"' in launcher
    assert "getusersitepackages" in launcher
    assert '"/.local/lib/"' in launcher

    environment = os.environ.copy()
    environment.pop("MEM0_TELEMETRY", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import memory_monitor.app; "
                "from mem0.memory import telemetry; "
                "assert telemetry.MEM0_TELEMETRY is False; "
                "assert telemetry.client_telemetry.posthog is None"
            ),
        ],
        cwd=_REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_all_pipeline_operations_reject_a_turn_from_another_session(tmp_path):
    pipeline, repository, memory, session_a, turn = _pipeline(tmp_path)
    session_b = repository.create_session("simulation-1", "user-2", "run-2")
    before_steps = repository.list_steps(turn["turn_id"])
    before_state = deepcopy(memory.state)
    operations = [
        lambda: pipeline.run_next_step(turn["turn_id"], session_id=session_b["session_id"]),
        lambda: pipeline.run_step(
            turn["turn_id"],
            PipelineStep.CAPTURE_INPUT,
            session_id=session_b["session_id"],
        ),
        lambda: pipeline.retry_step(
            turn["turn_id"],
            PipelineStep.CAPTURE_INPUT,
            session_id=session_b["session_id"],
        ),
        lambda: pipeline.skip_step(
            turn["turn_id"],
            PipelineStep.RUN_MIDTERM,
            session_id=session_b["session_id"],
        ),
        lambda: pipeline.run_until(
            turn["turn_id"],
            PipelineStep.GENERATE_RESPONSE,
            session_id=session_b["session_id"],
        ),
        lambda: pipeline.run_all(turn["turn_id"], session_id=session_b["session_id"]),
        lambda: pipeline.reset_turn(turn["turn_id"], session_id=session_b["session_id"]),
    ]

    for operation in operations:
        with pytest.raises(TurnSessionMismatchError, match="does not belong"):
            operation()

    assert repository.assert_turn_belongs_to_session(turn["turn_id"], session_a["session_id"]) == turn
    assert repository.list_steps(turn["turn_id"]) == before_steps
    assert memory.state == before_state


def test_demo_database_has_separate_tables_and_preserves_raw_messages(tmp_path):
    pipeline, repository, _, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "raw-messages")

    with repository._connection() as connection:
        table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"demo_sessions", "demo_turns", "demo_step_runs", "demo_snapshots"} <= table_names
    assert len(repository.list_steps(turn["turn_id"])) == len(PIPELINE_STEPS)
    assert repository.raw_messages(session["session_id"]) == [
        {
            "turn_id": turn["turn_id"],
            "role": "user",
            "content": "What changed?",
            "created_at": turn["created_at"],
        }
    ]

    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        messages = repository.raw_messages(session["session_id"])
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[1]["content"] == "model answer"
    finally:
        coordinator.shutdown(wait=True)


def test_steps_run_in_order_and_prompt_sent_matches_persisted_prompt(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "ordered")
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        steps = repository.list_steps(turn["turn_id"])
        assert [step["status"] for step in steps[:4]] == [StepStatus.SUCCEEDED.value] * 4
        assert [step["status"] for step in steps[4:]] == [StepStatus.PENDING.value] * 4
        prompt_output = repository.get_step(turn["turn_id"], PipelineStep.BUILD_PROMPT)["output"]
        generation_input = repository.get_step(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)["input"]
        assert prompt_output["messages"] == memory.generated_messages
        assert generation_input["messages"] == memory.generated_messages
        assert prompt_output["context_hash"] == generation_input["context_hash"]
        assert memory.retrieve_calls == memory.build_calls == memory.generation_calls == 1
    finally:
        coordinator.shutdown(wait=True)


def test_slow_pipeline_operations_never_execute_on_the_calling_thread(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "thread-boundary")
    caller_thread = threading.get_ident()
    execution_threads = {}

    def record(name, method):
        def wrapped(*args, **kwargs):
            execution_threads[name] = threading.get_ident()
            return method(*args, **kwargs)

        return wrapped

    memory.retrieve_context_for_demo = record("retrieve", memory.retrieve_context_for_demo)
    memory.build_prompt_from_context = record("prompt", memory.build_prompt_from_context)
    memory.generate_response_for_demo = record("model", memory.generate_response_for_demo)
    memory.commit_demo_turn = record("memory_add", memory.commit_demo_turn)
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        assert set(execution_threads) == {"retrieve", "prompt", "model", "memory_add"}
        assert all(thread_id != caller_thread for thread_id in execution_threads.values())
    finally:
        coordinator.shutdown(wait=True)


def test_next_step_runs_four_foreground_clicks_then_one_memory_batch(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "next")
    try:
        for step in PIPELINE_STEPS[:4]:
            result = pipeline.run_next_step(turn["turn_id"], session_id=session["session_id"])
            assert list(result["submissions"]) == [step.value]
            assert coordinator.wait_for_idle(3)

        result = pipeline.run_next_step(turn["turn_id"], session_id=session["session_id"])
        assert list(result["submissions"]) == [PipelineStep.RUN_SHORTTERM.value]
        assert coordinator.wait_for_idle(3)

        memory_steps = repository.list_steps(turn["turn_id"])[4:]
        assert [step["status"] for step in memory_steps] == [StepStatus.SUCCEEDED.value] * 4
        assert [step["attempts"] for step in memory_steps] == [1, 1, 1, 1]
        assert memory.commit_calls == 1
        assert memory.demo_background_worker.calls == {
            "midterm": ["migration-1"],
            "longterm": ["migration-1"],
            "profile": ["profile-1"],
        }
        assert repository.get_turn(turn["turn_id"])["completed_at"] is not None
        assert pipeline.run_next_step(turn["turn_id"], session_id=session["session_id"])["complete"]
    finally:
        coordinator.shutdown(wait=True)


def test_holding_shortterm_waits_without_blocking_other_gate_choices_then_fans_out_on_release(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "shortterm-held")
    pipeline.hold_step(
        turn["turn_id"],
        PipelineStep.RUN_SHORTTERM,
        session_id=session["session_id"],
    )
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        assert memory.commit_calls == 0
        assert memory.demo_background_worker.calls == {
            "midterm": [],
            "longterm": [],
            "profile": [],
        }
        memory_steps = repository.list_steps(turn["turn_id"])[4:]
        assert [step["status"] for step in memory_steps] == ["pending"] * 4
        assert [step["is_held"] for step in memory_steps] == [True, False, False, False]
        assert repository.get_turn(turn["turn_id"])["completed_at"] is None
        assert repository.get_turn(turn["turn_id"])["execution_target"] == "all"

        resumed = pipeline.set_memory_step_runnable(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            True,
            session_id=session["session_id"],
        )
        assert resumed["resumed"] is True
        assert coordinator.wait_for_idle(3)

        assert [step["status"] for step in repository.list_steps(turn["turn_id"])[4:]] == ["succeeded"] * 4
        assert memory.commit_calls == 1
        assert memory.demo_background_worker.calls == {
            "midterm": ["migration-1"],
            "longterm": ["migration-1"],
            "profile": ["profile-1"],
        }
        assert repository.get_turn(turn["turn_id"])["completed_at"] is not None
    finally:
        coordinator.shutdown(wait=True)


def test_duplicate_run_all_calls_memory_add_once_and_keeps_history(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "idempotent")
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])

        assert memory.commit_calls == 1
        assert len(memory.state["short_term"]) == 2
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)["attempts"] == 1
        assert repository.raw_messages(session["session_id"])[-1]["content"] == "model answer"
        assert repository.get_turn(turn["turn_id"])["execution_target"] is None
    finally:
        coordinator.shutdown(wait=True)


def test_model_answer_survives_shortterm_failure_and_retry_does_not_regenerate(tmp_path):
    memory = _FakeDemoMemory(commit_failures=1)
    pipeline, repository, _, session, turn = _pipeline(tmp_path, memory=memory)
    coordinator = _coordinator(pipeline, repository, "shortterm-retry")
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        stored_turn = repository.get_turn(turn["turn_id"])
        assert stored_turn["assistant_message"] == "model answer"
        assert repository.get_step(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)["status"] == "succeeded"
        shortterm = repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)
        assert shortterm["status"] == "failed"
        assert "temporary commit failure" in shortterm["error_message"]

        pipeline.retry_step(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            session_id=session["session_id"],
        )
        assert coordinator.wait_for_idle(3)

        assert memory.generation_calls == 1
        assert memory.commit_calls == 2
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)["attempts"] == 2
    finally:
        coordinator.shutdown(wait=True)


def test_repository_step_lease_blocks_a_second_page(tmp_path):
    _, repository, _, _, turn = _pipeline(tmp_path)
    token = repository.claim_step(turn["turn_id"], PipelineStep.CAPTURE_INPUT)

    with pytest.raises(StepAlreadyRunningError, match="already running"):
        repository.claim_step(turn["turn_id"], PipelineStep.CAPTURE_INPUT)

    repository.complete_step(
        turn["turn_id"],
        PipelineStep.CAPTURE_INPUT,
        token,
        input_data={},
        output_data={},
        duration_ms=0,
        before_snapshot_id=None,
        after_snapshot_id=None,
        diff={},
    )


def test_held_steps_stay_pending_while_unheld_memory_snapshot_diff_is_persisted(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "snapshot")
    pipeline.hold_step(turn["turn_id"], PipelineStep.RUN_MIDTERM, session_id=session["session_id"])
    pipeline.hold_step(turn["turn_id"], PipelineStep.RUN_LONGTERM, session_id=session["session_id"])
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        shortterm = repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)
        assert len(shortterm["diff"]["short_term"]["added"]) == 2
        assert set(shortterm["diff"]) == {"short_term"}
        midterm = repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)
        longterm = repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)
        assert (midterm["status"], midterm["is_held"]) == ("pending", True)
        assert (longterm["status"], longterm["is_held"]) == ("pending", True)
        assert memory.demo_background_worker.jobs["migration-1"]["status"] == "pending"
        assert memory.demo_background_worker.jobs["profile-1"]["status"] == "succeeded"
        assert repository.get_turn(turn["turn_id"])["completed_at"] is None
    finally:
        coordinator.shutdown(wait=True)


def test_holding_midterm_and_profile_runs_only_shortterm_and_longterm(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "longterm-only")
    pipeline.hold_step(turn["turn_id"], PipelineStep.RUN_MIDTERM, session_id=session["session_id"])
    pipeline.hold_step(turn["turn_id"], PipelineStep.RUN_PROFILE, session_id=session["session_id"])
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)["status"] == "succeeded"
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "pending"
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)["status"] == "succeeded"
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_PROFILE)["status"] == "pending"
        assert memory.demo_background_worker.calls == {
            "midterm": [],
            "longterm": ["migration-1"],
            "profile": [],
        }
        assert memory.commit_calls == 1
        assert repository.get_turn(turn["turn_id"])["completed_at"] is None
    finally:
        coordinator.shutdown(wait=True)


def test_background_step_fails_until_complete_job_reaches_success(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "worker-failure")

    def leave_in_retry(job_id):
        memory.demo_background_worker.jobs[job_id]["status"] = "retry"
        memory.demo_background_worker.jobs[job_id]["midterm_status"] = "retry"
        return True

    memory.demo_background_worker.process_midterm_job = leave_in_retry
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        failed = repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)
        assert failed["status"] == "failed"
        assert "status=retry" in failed["error_message"]
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)["status"] == "succeeded"
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_PROFILE)["status"] == "succeeded"
    finally:
        coordinator.shutdown(wait=True)


def test_pipeline_progress_recovers_from_a_new_repository_instance(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    first_coordinator = _coordinator(pipeline, repository, "before-reopen")
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert first_coordinator.wait_for_idle(3)
    finally:
        first_coordinator.shutdown(wait=True)

    reopened = DemoRepository(repository.db_path)
    resumed_coordinator = DemoBackgroundCoordinator("after-reopen", reopened)
    resumed = DemoPipelineService(
        memory,
        reopened,
        _FakeStateService(memory),
        coordinator=resumed_coordinator,
    )
    try:
        resumed.run_remaining(turn["turn_id"], session_id=session["session_id"])
        assert resumed_coordinator.wait_for_idle(3)

        assert reopened.get_turn(turn["turn_id"])["session_id"] == session["session_id"]
        assert reopened.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)["status"] == "succeeded"
        assert memory.retrieve_calls == 1
        assert memory.build_calls == 1
    finally:
        resumed_coordinator.shutdown(wait=True)


def test_application_restart_recovers_expired_queue_and_resumes_persisted_target(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    repository.set_execution_target(turn["turn_id"], "all")
    token = repository.queue_step(
        turn["turn_id"],
        PipelineStep.CAPTURE_INPUT,
        lease_seconds=1,
    )
    assert token
    with repository._connection() as connection:
        connection.execute(
            """
            UPDATE demo_step_runs
            SET lease_expires_at = '2000-01-01T00:00:00+00:00'
            WHERE turn_id = ? AND step = ?
            """,
            (turn["turn_id"], PipelineStep.CAPTURE_INPUT.value),
        )
        connection.commit()

    reopened = DemoRepository(repository.db_path)
    coordinator = DemoBackgroundCoordinator("restart", reopened)
    resumed = DemoPipelineService(
        memory,
        reopened,
        _FakeStateService(memory),
        coordinator=coordinator,
    )
    try:
        resumed.resume_pending_work()
        assert coordinator.wait_for_idle(3)
        assert reopened.get_turn(turn["turn_id"])["completed_at"] is not None
        assert memory.commit_calls == 1
    finally:
        coordinator.shutdown(wait=True)


def test_commit_step_recovers_after_core_success_but_demo_status_write_fails(tmp_path, monkeypatch):
    persisted_commits = {}
    memory = _PersistentlyIdempotentFakeMemory(persisted_commits)
    pipeline, repository, _, session, turn = _pipeline(tmp_path, memory=memory)
    coordinator = _coordinator(pipeline, repository, "status-crash")
    original_complete_step = repository.complete_step
    crashed = False

    def fail_after_core_commit(turn_id, step, token, **kwargs):
        nonlocal crashed
        if PipelineStep(step) is PipelineStep.RUN_SHORTTERM and not crashed:
            crashed = True
            raise RuntimeError("demo status write crashed")
        return original_complete_step(turn_id, step, token, **kwargs)

    monkeypatch.setattr(repository, "complete_step", fail_after_core_commit)
    pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
    assert coordinator.wait_for_idle(3)
    coordinator.shutdown(wait=True)

    assert repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)["status"] == "failed"
    assert len(memory.state["short_term"]) == 2

    monkeypatch.setattr(repository, "complete_step", original_complete_step)
    reopened = DemoRepository(repository.db_path)
    resumed_memory = _PersistentlyIdempotentFakeMemory(persisted_commits, shared_state=memory.state)
    resumed_coordinator = DemoBackgroundCoordinator("status-recovery", reopened)
    resumed = DemoPipelineService(
        resumed_memory,
        reopened,
        _FakeStateService(resumed_memory),
        coordinator=resumed_coordinator,
    )
    try:
        resumed.retry_step(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            session_id=session["session_id"],
        )
        assert resumed_coordinator.wait_for_idle(3)

        assert reopened.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)["status"] == "succeeded"
        assert len(resumed_memory.state["short_term"]) == 2
        assert len(resumed_memory.state["jobs"]["migration"]) == 1
        assert len(resumed_memory.state["jobs"]["profile"]) == 1
        assert resumed_memory.commit_kwargs[0]["simulation_id"] == "simulation-1"
        assert resumed_memory.commit_kwargs[0]["turn_id"] == turn["turn_id"]
    finally:
        resumed_coordinator.shutdown(wait=True)


def test_reset_before_memory_add_clears_progress_but_memory_add_is_protected(tmp_path):
    pipeline, repository, _, session, turn = _pipeline(tmp_path)
    coordinator = _coordinator(pipeline, repository, "reset")
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        pipeline.reset_turn(turn["turn_id"], session_id=session["session_id"])

        assert {step["status"] for step in repository.list_steps(turn["turn_id"])} == {"pending"}
        assert repository.get_turn(turn["turn_id"])["assistant_message"] is None

        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        with pytest.raises(RuntimeError, match="cannot be safely reset"):
            pipeline.reset_turn(turn["turn_id"], session_id=session["session_id"])
    finally:
        coordinator.shutdown(wait=True)


def test_demo_databases_isolate_simulation_user_and_run(tmp_path):
    first = DemoRepository(tmp_path / "one" / "demo.db")
    second = DemoRepository(tmp_path / "two" / "demo.db")
    first_session = first.create_session("simulation-1", "user-1", "run-1")
    second_session = second.create_session("simulation-2", "user-2", "run-2")
    first.create_turn(
        first_session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="first",
    )
    second.create_turn(
        second_session["session_id"],
        user_id="user-2",
        run_id="run-2",
        user_message="second",
    )

    assert [item["content"] for item in first.raw_messages(first_session["session_id"])] == ["first"]
    assert [item["content"] for item in second.raw_messages(second_session["session_id"])] == ["second"]
    with pytest.raises(ValueError, match="scope"):
        first.create_turn(
            first_session["session_id"],
            user_id="user-2",
            run_id="run-1",
            user_message="leak",
        )


class _SimulationMemory:
    def __init__(self, config):
        self.config = config
        self.vector_store = SimpleNamespace(list=lambda **kwargs: [])
        self.closed = False

    def close(self):
        self.closed = True


def test_simulation_service_reopens_existing_sandbox_without_monkey_patch(tmp_path):
    service = SimulationService(
        tmp_path / "runs",
        memory_factory=_SimulationMemory,
    )
    environment = service.create_environment("sandbox-1")
    marker = environment.root / "preserved.txt"
    marker.write_text("keep", encoding="utf-8")
    assert (environment.root / "demo.db").exists()
    assert environment.memory.config.history_db_path == str(environment.root / "history.db")

    environment.coordinator.shutdown(wait=True)
    environment.memory.close()
    service._environments.clear()
    reopened = service.environment("sandbox-1")

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not hasattr(reopened, "midterm_handler")
    assert not hasattr(reopened, "longterm_handler")
    service.close()


def test_simulation_service_clear_closes_coordinator_and_memory_before_delete(tmp_path):
    service = SimulationService(
        tmp_path / "runs",
        memory_factory=_SimulationMemory,
    )
    environment = service.create_environment("delete-me")
    root = environment.root

    service.clear_environment("delete-me")

    assert environment.memory.closed
    assert not environment.coordinator.accepting
    assert not root.exists()
    assert "delete-me" not in service._environments


def test_simulation_service_preserves_qdrant_bm25_language(tmp_path):
    base_config = MemoryConfig.model_validate(
        {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "embedding_model_dims": 512,
                    "bm25_language": "zh",
                },
            }
        }
    )
    service = SimulationService(
        tmp_path / "runs",
        base_config=base_config,
        memory_factory=_SimulationMemory,
    )

    environment = service.create_environment("sandbox-zh")

    assert environment.memory.config.vector_store.config.embedding_model_dims == 512
    assert environment.memory.config.vector_store.config.bm25_language == "zh"


def test_memory_state_diff_reports_added_updated_and_deleted_records():
    before = {
        "short_term": [{"id": "one", "status": "active"}, {"id": "deleted", "status": "active"}],
        "midterm_sessions": [],
        "midterm_pages": [],
        "long_term": [],
        "profile": [],
        "jobs": {"migration": [], "profile": []},
    }
    after = {
        "short_term": [{"id": "one", "status": "pending"}, {"id": "two", "status": "active"}],
        "midterm_sessions": [],
        "midterm_pages": [],
        "long_term": [],
        "profile": [],
        "jobs": {"migration": [], "profile": []},
    }

    diff = MemoryStateService.compare(before, after)["short_term"]

    assert [row["id"] for row in diff["added"]] == ["two"]
    assert [row["id"] for row in diff["updated"]] == ["one"]
    assert [row["id"] for row in diff["deleted"]] == ["deleted"]


def test_latest_session_snapshot_survives_selecting_a_new_turn_without_snapshots(tmp_path):
    repository = DemoRepository(tmp_path / "session-snapshot.db")
    session = repository.create_session("snapshot-session", "user-1", "run-1")
    first = repository.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="first",
    )
    expected = {**deepcopy(_FakeDemoMemory().state), "short_term": [{"id": "message-1", "status": "active"}]}
    repository.create_snapshot(first["turn_id"], PipelineStep.RUN_SHORTTERM, "after", expected)
    repository.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="second without snapshot",
    )

    latest = repository.latest_session_snapshot(session["session_id"])
    environment = SimpleNamespace(
        simulation_id="snapshot-session",
        repository=repository,
        state_service=SimpleNamespace(
            snapshot=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("persisted snapshot must win"))
        ),
    )
    displayed = demo_lab._latest_session_state(
        SimpleNamespace(session_state={}),
        environment,
        session["session_id"],
    )

    assert latest["turn_id"] == first["turn_id"]
    assert latest["data"]["short_term"] == [{"id": "message-1", "status": "active"}]
    assert displayed["short_term"] == [{"id": "message-1", "status": "active"}]


def test_repository_reads_messages_steps_and_latest_snapshot_after_multiple_turns(tmp_path):
    repository = DemoRepository(tmp_path / "multiple-turns.db")
    session = repository.create_session("multiple-turns", "user-1", "run-1")
    turns = []
    for index in range(1, 5):
        turn = repository.create_turn(
            session["session_id"],
            user_id="user-1",
            run_id="run-1",
            user_message=f"question-{index}",
        )
        with repository._connection() as connection:
            connection.execute(
                "UPDATE demo_turns SET assistant_message = ?, updated_at = ? WHERE turn_id = ?",
                (f"answer-{index}", f"2026-07-30T12:00:0{index}+00:00", turn["turn_id"]),
            )
            connection.commit()
        repository.create_snapshot(
            turn["turn_id"],
            PipelineStep.RUN_SHORTTERM,
            "after",
            {**deepcopy(_FakeDemoMemory().state), "round": index},
        )
        turns.append(turn)

    messages = repository.raw_messages(session["session_id"])
    assert [message["content"] for message in messages] == [
        "question-1",
        "answer-1",
        "question-2",
        "answer-2",
        "question-3",
        "answer-3",
        "question-4",
        "answer-4",
    ]
    assert all(len(repository.list_steps(turn["turn_id"])) == len(PIPELINE_STEPS) for turn in turns)
    assert repository.latest_session_snapshot(session["session_id"])["data"]["round"] == 4


def test_memory_state_uses_core_session_scope_and_only_lists_active_messages(tmp_path):
    history_path = tmp_path / "history.db"
    db = SQLiteManager(str(history_path))
    scope = DemoMemory.session_scope_for_demo(user_id="user-1", run_id="run-1")
    try:
        db.save_messages(
            [{"role": "user", "content": "persisted question"}],
            scope,
            max_messages=10,
        )
        db.connection.execute(
            """
            INSERT INTO messages (id, session_scope, role, content, status)
            VALUES ('pending-message', ?, 'assistant', 'migrating answer', 'pending')
            """,
            (scope,),
        )
        db.connection.commit()
        memory = SimpleNamespace(
            config=SimpleNamespace(
                history_db_path=str(history_path),
                midterm=SimpleNamespace(enabled=False),
            ),
            vector_store=SimpleNamespace(list=lambda **_kwargs: []),
            session_scope_for_demo=DemoMemory.session_scope_for_demo,
        )

        snapshot = MemoryStateService(memory).snapshot(user_id="user-1", run_id="run-1")
        rows = db.connection.execute(
            """
            SELECT id, session_scope, role, content, status, created_at
            FROM messages ORDER BY created_at, rowid
            """
        ).fetchall()

        assert len(rows) == 2
        assert {row[1] for row in rows} == {scope}
        assert [message["content"] for message in snapshot["short_term"]] == ["persisted question"]
        assert snapshot["short_term"][0]["session_scope"] == scope
        assert snapshot["short_term"][0]["status"] == "active"
        assert set(snapshot) == {
            "short_term",
            "midterm_sessions",
            "midterm_pages",
            "long_term",
            "profile",
            "jobs",
        }
        assert set(snapshot["jobs"]) == {"migration", "profile"}
    finally:
        db.close()


def test_memory_state_partial_snapshot_only_reads_requested_backend(tmp_path):
    class Midterm:
        def __init__(self):
            self.page_calls = 0

        def list_sessions(self, **_kwargs):
            raise AssertionError("session store must not be read")

        def list_pages(self, **_kwargs):
            self.page_calls += 1
            return [SimpleNamespace(id="page-1", score=0.8, payload={"data": "page"})]

    class Longterm:
        def list(self, **_kwargs):
            raise AssertionError("long-term store must not be read")

    midterm = Midterm()
    memory = SimpleNamespace(
        config=SimpleNamespace(
            history_db_path=str(tmp_path / "missing-history.db"),
            midterm=SimpleNamespace(enabled=True),
        ),
        midterm_memory=midterm,
        vector_store=Longterm(),
    )

    snapshot = MemoryStateService(memory).snapshot(
        user_id="user-1",
        run_id="run-1",
        sections={"midterm_pages"},
    )

    assert snapshot == {
        "midterm_pages": [{"id": "page-1", "score": 0.8, "payload": {"data": "page"}}]
    }
    assert midterm.page_calls == 1


def test_memory_state_rejects_unknown_snapshot_section(tmp_path):
    memory = SimpleNamespace(
        config=SimpleNamespace(
            history_db_path=str(tmp_path / "history.db"),
            midterm=SimpleNamespace(enabled=False),
        )
    )

    with pytest.raises(ValueError, match="Unknown memory snapshot sections"):
        MemoryStateService(memory).snapshot(
            user_id="user-1",
            run_id="run-1",
            sections={"midterm_page"},
        )


def test_memory_state_partial_compare_only_reports_requested_sections():
    before = {
        "midterm_pages": [{"id": "mid-1", "payload": {"data": "old"}}],
        "long_term": [{"id": "long-1", "payload": {"data": "old"}}],
    }
    after = {
        "midterm_pages": [{"id": "mid-1", "payload": {"data": "new"}}],
        "long_term": [{"id": "long-2", "payload": {"data": "new"}}],
    }

    diff = MemoryStateService.compare(before, after, sections={"midterm_pages"})

    assert set(diff) == {"midterm_pages"}
    assert [row["id"] for row in diff["midterm_pages"]["updated"]] == ["mid-1"]

    missing_after = MemoryStateService.compare(
        before,
        {},
        sections={"midterm_pages"},
    )
    assert missing_after["midterm_pages"]["deleted"] == []


def test_pipeline_snapshot_section_mapping_excludes_shared_job_rows():
    assert STEP_SNAPSHOT_SECTIONS == {
        PipelineStep.RUN_SHORTTERM: frozenset({"short_term"}),
        PipelineStep.RUN_MIDTERM: frozenset({"midterm_sessions", "midterm_pages"}),
        PipelineStep.RUN_LONGTERM: frozenset({"long_term"}),
        PipelineStep.RUN_PROFILE: frozenset({"profile"}),
    }

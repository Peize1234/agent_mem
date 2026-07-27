import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from memory_monitor.models import PIPELINE_STEPS, PipelineStep, StepStatus
from memory_monitor.services.demo_pipeline_service import DemoPipelineService, PipelineStepError
from memory_monitor.services.demo_repository import DemoRepository, StepAlreadyRunningError
from memory_monitor.services.memory_state_service import MemoryStateService
from memory_monitor.services.simulation_service import SimulationService


class _FakeStateService:
    def __init__(self, memory):
        self.memory = memory

    def snapshot(self, *, user_id, run_id):
        state = deepcopy(self.memory.state)
        state["scope"] = {"user_id": user_id, "run_id": run_id}
        return state

    compare = staticmethod(MemoryStateService.compare)


class _FakeWorker:
    def __init__(self, memory):
        self.memory = memory
        self.jobs = {
            "migration-1": {"job_id": "migration-1", "status": "pending"},
            "profile-1": {"job_id": "profile-1", "status": "pending"},
        }

    def process_migration_job(self, job_id):
        job = self.jobs[job_id]
        if job["status"] == "succeeded":
            return False
        job["status"] = "succeeded"
        self.memory.state["jobs"]["migration"][0]["status"] = "succeeded"
        self.memory.state["long_term"].append({"id": "long-1", "payload": {"data": "durable"}})
        self.memory._events.append({"event_type": "job.finished", "job_id": job_id})
        return True

    def process_profile_job(self, job_id):
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


class _FakeDemoMemory:
    def __init__(self, *, commit_failures=0):
        self.retrieve_calls = 0
        self.build_calls = 0
        self.generation_calls = 0
        self.commit_calls = 0
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


def test_demo_database_has_separate_tables_and_preserves_raw_messages(tmp_path):
    pipeline, repository, _, session, turn = _pipeline(tmp_path)

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

    pipeline.run_until(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)

    messages = repository.raw_messages(session["session_id"])
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "model answer"


def test_steps_run_in_order_and_prompt_sent_matches_persisted_prompt(tmp_path):
    pipeline, repository, memory, _, turn = _pipeline(tmp_path)

    pipeline.run_until(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)

    steps = repository.list_steps(turn["turn_id"])
    assert [step["status"] for step in steps[:4]] == [StepStatus.SUCCEEDED.value] * 4
    assert [step["status"] for step in steps[4:]] == [StepStatus.PENDING.value] * 4
    prompt_output = repository.get_step(turn["turn_id"], PipelineStep.BUILD_PROMPT)["output"]
    generation_input = repository.get_step(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)["input"]
    assert prompt_output["messages"] == memory.generated_messages
    assert generation_input["messages"] == memory.generated_messages
    assert prompt_output["context_hash"] == generation_input["context_hash"]
    assert memory.retrieve_calls == memory.build_calls == memory.generation_calls == 1


def test_duplicate_execution_returns_persisted_result_without_side_effect(tmp_path):
    pipeline, repository, memory, _, turn = _pipeline(tmp_path)
    pipeline.run_until(turn["turn_id"], PipelineStep.COMMIT_TURN)

    first = repository.get_step(turn["turn_id"], PipelineStep.COMMIT_TURN)
    second = pipeline.run_step(turn["turn_id"], PipelineStep.COMMIT_TURN)

    assert second == first
    assert memory.commit_calls == 1
    assert len(memory.state["short_term"]) == 2


def test_model_answer_survives_commit_failure_and_commit_can_retry(tmp_path):
    memory = _FakeDemoMemory(commit_failures=1)
    pipeline, repository, _, _, turn = _pipeline(tmp_path, memory=memory)
    pipeline.run_until(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)

    with pytest.raises(PipelineStepError, match="commit_turn"):
        pipeline.run_step(turn["turn_id"], PipelineStep.COMMIT_TURN)

    stored_turn = repository.get_turn(turn["turn_id"])
    assert stored_turn["assistant_message"] == "model answer"
    assert repository.get_step(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)["status"] == "succeeded"
    assert repository.get_step(turn["turn_id"], PipelineStep.COMMIT_TURN)["status"] == "failed"

    pipeline.retry_step(turn["turn_id"], PipelineStep.COMMIT_TURN)

    assert memory.generation_calls == 1
    assert memory.commit_calls == 2
    assert repository.get_step(turn["turn_id"], PipelineStep.COMMIT_TURN)["attempts"] == 2


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


def test_optional_steps_skip_and_full_pipeline_snapshot_diff(tmp_path):
    pipeline, repository, memory, _, turn = _pipeline(tmp_path)
    pipeline.run_until(turn["turn_id"], PipelineStep.COMMIT_TURN)

    commit_step = repository.get_step(turn["turn_id"], PipelineStep.COMMIT_TURN)
    assert len(commit_step["diff"]["short_term"]["added"]) == 2
    assert commit_step["diff"]["migration_jobs"]["added"][0]["job_id"] == "migration-1"

    skipped = pipeline.skip_step(turn["turn_id"], PipelineStep.RUN_MIGRATION, reason="inspect profile only")
    assert skipped["status"] == StepStatus.SKIPPED.value
    pipeline.run_until(turn["turn_id"], PipelineStep.REFRESH_STATE)

    assert memory.demo_background_worker.jobs["migration-1"]["status"] == "pending"
    assert memory.demo_background_worker.jobs["profile-1"]["status"] == "succeeded"
    assert repository.get_step(turn["turn_id"], PipelineStep.REFRESH_STATE)["status"] == "succeeded"


def test_background_step_fails_until_complete_job_reaches_success(tmp_path):
    pipeline, repository, memory, _, turn = _pipeline(tmp_path)
    pipeline.run_until(turn["turn_id"], PipelineStep.COMMIT_TURN)

    def leave_in_retry(job_id):
        memory.demo_background_worker.jobs[job_id]["status"] = "retry"
        return True

    memory.demo_background_worker.process_migration_job = leave_in_retry

    with pytest.raises(PipelineStepError, match="status=retry"):
        pipeline.run_step(turn["turn_id"], PipelineStep.RUN_MIGRATION)

    assert repository.get_step(turn["turn_id"], PipelineStep.RUN_MIGRATION)["status"] == "failed"


def test_pipeline_progress_recovers_from_a_new_repository_instance(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    pipeline.run_until(turn["turn_id"], PipelineStep.BUILD_PROMPT)

    reopened = DemoRepository(repository.db_path)
    resumed = DemoPipelineService(memory, reopened, _FakeStateService(memory))
    resumed.run_until(turn["turn_id"], PipelineStep.COMMIT_TURN)

    assert reopened.get_turn(turn["turn_id"])["session_id"] == session["session_id"]
    assert reopened.get_step(turn["turn_id"], PipelineStep.COMMIT_TURN)["status"] == "succeeded"
    assert memory.retrieve_calls == 1
    assert memory.build_calls == 1


def test_reset_turn_before_commit_clears_progress_but_committed_turn_is_protected(tmp_path):
    pipeline, repository, _, _, turn = _pipeline(tmp_path)
    pipeline.run_until(turn["turn_id"], PipelineStep.BUILD_PROMPT)

    pipeline.reset_turn(turn["turn_id"])

    assert {step["status"] for step in repository.list_steps(turn["turn_id"])} == {"pending"}
    assert repository.get_turn(turn["turn_id"])["assistant_message"] is None

    pipeline.run_until(turn["turn_id"], PipelineStep.COMMIT_TURN)
    with pytest.raises(RuntimeError, match="cannot be reset"):
        pipeline.reset_turn(turn["turn_id"])


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

    service._environments.clear()
    reopened = service.environment("sandbox-1")

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not hasattr(reopened, "midterm_handler")
    assert not hasattr(reopened, "longterm_handler")


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

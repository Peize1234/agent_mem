from copy import deepcopy
from types import SimpleNamespace

from mem0.configs.base import BackgroundTaskConfig
from mem0.memory.storage import SQLiteManager
from memory_monitor.runtime.demo_background_worker import DemoBackgroundWorkerManager
from memory_monitor.runtime.demo_memory import DemoMemory
from memory_monitor.models import PipelineStep
from memory_monitor.runtime import DemoBackgroundCoordinator
from memory_monitor.services.demo_pipeline_service import DemoPipelineService
from memory_monitor.services.demo_repository import DemoRepository
from memory_monitor.services.memory_state_service import MemoryStateService

from tests.memory_monitor.test_demo_pipeline import _FakeDemoMemory, _FakeWorker


def _worker(db, **handlers):
    return DemoBackgroundWorkerManager(
        db,
        BackgroundTaskConfig(enabled=True, max_retries=0),
        process_midterm=handlers.get("midterm", lambda *_args: None),
        process_longterm=handlers.get("longterm", lambda *_args: None),
        process_profile=handlers.get("profile", lambda *_args: None),
        process_longterm_extraction=handlers.get("extraction", lambda *_args: None),
        process_promotion=handlers.get("promotion", lambda *_args: None),
    )


def test_state_service_splits_fine_grained_and_promoted_longterm(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    scope = DemoMemory.session_scope_for_demo(user_id="u", run_id="r")
    db.save_messages_and_create_background_jobs(
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        scope,
        max_messages=0,
        filters={"user_id": "u", "run_id": "r"},
        metadata={},
        infer=False,
        prompt=None,
        profile_user_id="u",
        create_longterm_jobs=True,
        return_longterm_job_ids=True,
    )
    memory = SimpleNamespace(
        config=SimpleNamespace(
            history_db_path=str(tmp_path / "history.db"),
            midterm=SimpleNamespace(enabled=False),
        ),
        db=db,
        vector_store=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    id="fine-1",
                    score=None,
                    payload={"source_job_type": "longterm_extraction", "data": "fact"},
                )
            ]
        ),
        promoted_longterm=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    id="promoted-1",
                    score=None,
                    payload={"source": "cross_session_long_term", "memory": "summary"},
                )
            ]
        ),
        session_scope_for_demo=DemoMemory.session_scope_for_demo,
    )
    try:
        state = MemoryStateService(memory).current_state(user_id="u", run_id="r")
        assert [row["id"] for row in state["fine_grained_longterm"]] == ["fine-1"]
        assert [row["id"] for row in state["promoted_longterm"]] == ["promoted-1"]
        assert {"longterm_extraction", "promotion", "migration", "profile"} == set(state["jobs"])
    finally:
        db.close()


def test_demo_worker_drives_longterm_extraction_job_with_core_state(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    scope = DemoMemory.session_scope_for_demo(user_id="u", run_id="r")
    _migration, _profile, extraction_ids = db.save_messages_and_create_background_jobs(
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        scope,
        max_messages=0,
        filters={"user_id": "u", "run_id": "r"},
        metadata={},
        infer=False,
        prompt=None,
        profile_user_id=None,
        create_longterm_jobs=True,
        return_longterm_job_ids=True,
    )
    calls = []
    worker = _worker(db, extraction=lambda job: calls.append(job["job_id"]))
    try:
        worker.start()
        assert worker.process_longterm_extraction_job(extraction_ids[0]) is True
        assert calls == extraction_ids[:1]
        assert db.get_background_job(extraction_ids[0], "longterm_extraction")["status"] == "succeeded"
    finally:
        worker.stop()
        db.close()


def test_demo_worker_drives_promotion_job_with_core_state(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    job = db.ensure_promotion_job(
        user_id="u",
        source_midterm_session_id="session-1",
        source_run_id="run-1",
        source_version="version-1",
    )
    calls = []
    worker = _worker(db, promotion=lambda item: calls.append(item["job_id"]))
    try:
        worker.start()
        assert worker.process_promotion_job(job["job_id"]) is True
        assert calls == [job["job_id"]]
        assert db.get_background_job(job["job_id"], "promotion")["status"] == "succeeded"
    finally:
        worker.stop()
        db.close()


class _PipelineStateService:
    """Small persisted-state adapter for exercising PipelineService branches."""

    def __init__(self, memory):
        self.memory = memory

    def snapshot(self, *, user_id, run_id, sections=None):
        state = deepcopy(self.memory.state)
        if sections is None:
            return state
        selected = set(sections)
        snapshot = {section: state[section] for section in selected if section in state}
        job_sections = {
            "migration_jobs": "migration",
            "longterm_extraction_jobs": "longterm_extraction",
            "profile_jobs": "profile",
            "promotion_jobs": "promotion",
        }
        requested_jobs = selected.intersection(job_sections)
        if requested_jobs:
            snapshot["jobs"] = {
                job_sections[section]: state["jobs"][job_sections[section]]
                for section in requested_jobs
            }
        return snapshot

    compare = staticmethod(MemoryStateService.compare)


class _PipelineWorker(_FakeWorker):
    def __init__(self, memory, *, migration=False, extraction=False, promotion=False):
        super().__init__(memory)
        self.calls.setdefault("extraction", [])
        self.calls.setdefault("promotion", [])
        self.memory.state["jobs"].setdefault("longterm_extraction", [])
        self.memory.state["jobs"].setdefault("promotion", [])
        if extraction:
            self.jobs["extraction-1"] = {
                "job_id": "extraction-1",
                "status": "pending",
            }
            self.memory.state["jobs"]["longterm_extraction"].append(self.jobs["extraction-1"])
        if not migration:
            self.jobs.pop("migration-1", None)
            self.jobs.pop("profile-1", None)
            self.memory.state["jobs"]["migration"] = []
            self.memory.state["jobs"]["profile"] = []
        if promotion:
            self.jobs["promotion-1"] = {
                "job_id": "promotion-1",
                "status": "pending",
                "source_midterm_session_id": "midterm-session-other-turn",
                "source_run_id": "run-other-turn",
            }
            self.memory.state["jobs"]["promotion"].append(self.jobs["promotion-1"])

    def process_longterm_extraction_job(self, job_id):
        self.calls["extraction"].append(job_id)
        job = self.jobs[job_id]
        if job["status"] == "succeeded":
            return False
        job["status"] = "succeeded"
        self.memory.state["jobs"]["longterm_extraction"][0].update(deepcopy(job))
        self.memory.state["fine_grained_longterm"].append(
            {"id": "fine-fact-1", "payload": {"source_job_type": "longterm_extraction"}}
        )
        return True

    def process_next_promotion_job_details(self):
        job = self.jobs.get("promotion-1")
        if job is None or job["status"] != "pending":
            return None
        self.calls["promotion"].append(job["job_id"])
        job["status"] = "succeeded"
        self.memory.state["jobs"]["promotion"][0].update(deepcopy(job))
        self.memory.state["promoted_longterm"].append(
            {
                "id": "promoted-fact-1",
                "payload": {
                    "source": "cross_session_long_term",
                    "source_midterm_session_id": job["source_midterm_session_id"],
                },
            }
        )
        return {
            "job_type": "promotion",
            "job_id": job["job_id"],
            "source_midterm_session_id": job["source_midterm_session_id"],
            "source_run_id": job["source_run_id"],
            "status": job["status"],
            "processed": True,
            "queue_scope": "core_promotion_queue",
        }


class _PipelineMemory(_FakeDemoMemory):
    def __init__(self, *, migration=False, extraction=False, promotion=False):
        super().__init__()
        self.state["fine_grained_longterm"] = []
        self.state["promoted_longterm"] = []
        self.state["jobs"].setdefault("longterm_extraction", [])
        self.state["jobs"].setdefault("promotion", [])
        self.demo_background_worker = _PipelineWorker(
            self,
            migration=migration,
            extraction=extraction,
            promotion=promotion,
        )
        self._migration = migration
        self._extraction = extraction

    def commit_demo_turn(self, **kwargs):
        self.commit_calls += 1
        self.state["short_term"].extend(
            [
                {"id": "message-user", "role": "user", "content": kwargs["user_message"]},
                {"id": "message-assistant", "role": "assistant", "content": kwargs["assistant_message"]},
            ]
        )
        background = {
            "migration_job_id": "migration-1" if self._migration else None,
            "longterm_extraction_job_ids": ["extraction-1"] if self._extraction else [],
            "profile_job_id": None,
        }
        if self._migration:
            self.state["jobs"]["migration"] = [deepcopy(self.demo_background_worker.jobs["migration-1"])]
        return {"results": [], "background": background}


def _pipeline_turn(tmp_path, memory):
    repository = DemoRepository(tmp_path / "pipeline.db")
    session = repository.create_session("simulation-1", "user-1", "run-1")
    turn = repository.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="What changed?",
    )
    pipeline = DemoPipelineService(memory, repository, _PipelineStateService(memory))
    coordinator = DemoBackgroundCoordinator("longterm-pipeline", repository)
    pipeline.coordinator = coordinator
    return pipeline, repository, coordinator, session, turn


def _complete_answer_and_shortterm(pipeline, coordinator, session, turn):
    pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
    assert coordinator.wait_for_idle(3)
    pipeline.run_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM, session_id=session["session_id"])
    assert coordinator.wait_for_idle(3)


def test_pipeline_longterm_runs_extraction_without_migration_job(tmp_path):
    memory = _PipelineMemory(migration=False, extraction=True)
    pipeline, repository, coordinator, session, turn = _pipeline_turn(tmp_path, memory)
    try:
        _complete_answer_and_shortterm(pipeline, coordinator, session, turn)
        pipeline.run_step(turn["turn_id"], PipelineStep.RUN_LONGTERM, session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        step = repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)
        assert step["status"] == "succeeded"
        assert memory.demo_background_worker.calls["extraction"] == ["extraction-1"]
        assert memory.demo_background_worker.jobs["extraction-1"]["status"] == "succeeded"
        assert memory.state["fine_grained_longterm"]
        assert set(step["diff"]) == {"fine_grained_longterm", "longterm_extraction_jobs"}
    finally:
        coordinator.shutdown(wait=True)


def test_pipeline_midterm_can_drive_unrelated_promotion_queue(tmp_path):
    memory = _PipelineMemory(migration=False, promotion=True)
    pipeline, repository, coordinator, session, turn = _pipeline_turn(tmp_path, memory)
    try:
        _complete_answer_and_shortterm(pipeline, coordinator, session, turn)
        pipeline.run_step(turn["turn_id"], PipelineStep.RUN_MIDTERM, session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        step = repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)
        assert step["status"] == "succeeded"
        output = step["output"]
        assert output["promotion_processed"] is True
        assert output["promotion_job_id"] == "promotion-1"
        assert output["promotion_source_midterm_session_id"] == "midterm-session-other-turn"
        assert memory.state["promoted_longterm"]
        assert set(step["diff"]) == {
            "midterm_sessions",
            "midterm_pages",
            "promotion_jobs",
            "promoted_longterm",
        }
    finally:
        coordinator.shutdown(wait=True)


def test_pipeline_longterm_processes_migration_and_extraction_once(tmp_path):
    memory = _PipelineMemory(migration=True, extraction=True)
    pipeline, repository, coordinator, session, turn = _pipeline_turn(tmp_path, memory)
    try:
        _complete_answer_and_shortterm(pipeline, coordinator, session, turn)
        pipeline.run_step(turn["turn_id"], PipelineStep.RUN_LONGTERM, session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        step = repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)
        assert step["status"] == "succeeded"
        assert memory.demo_background_worker.calls["longterm"] == ["migration-1"]
        assert memory.demo_background_worker.calls["extraction"] == ["extraction-1"]
        assert step["output"]["job_ids"] == ["migration-1", "extraction-1"]
    finally:
        coordinator.shutdown(wait=True)

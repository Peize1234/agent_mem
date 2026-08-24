from types import SimpleNamespace

from mem0.configs.base import BackgroundTaskConfig
from mem0.memory.storage import SQLiteManager
from memory_monitor.runtime.demo_background_worker import DemoBackgroundWorkerManager
from memory_monitor.runtime.demo_memory import DemoMemory
from memory_monitor.services.memory_state_service import MemoryStateService


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

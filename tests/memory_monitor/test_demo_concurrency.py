import sqlite3
import threading

import pytest

from memory_monitor.components import pipeline_graph
from memory_monitor.models import (
    BACKGROUND_STEPS,
    STEP_DEPENDENCIES,
    BackgroundStepConfig,
    PipelineStep,
    dependencies_for,
)
from memory_monitor.runtime import DemoBackgroundCoordinator
from memory_monitor.services.demo_pipeline_service import DemoPipelineService
from memory_monitor.services.demo_repository import DemoRepository
from tests.memory_monitor.test_demo_pipeline import _pipeline


def _turn(repository, session, label, *, config=None):
    return repository.create_turn(
        session["session_id"],
        user_id=session["user_id"],
        run_id=session["run_id"],
        user_message=label,
        background_config=config,
    )


def _complete(repository, turn_id, step, token):
    repository.complete_step(
        turn_id,
        step,
        token,
        input_data={},
        output_data={"step": step.value},
        duration_ms=0,
        before_snapshot_id=None,
        after_snapshot_id=None,
        diff={},
    )


def test_dag_dependencies_are_explicit_and_refresh_is_dynamic():
    assert STEP_DEPENDENCIES[PipelineStep.RUN_MIDTERM] == (PipelineStep.COMMIT_TURN,)
    assert STEP_DEPENDENCIES[PipelineStep.RUN_LONGTERM] == (PipelineStep.COMMIT_TURN,)
    assert STEP_DEPENDENCIES[PipelineStep.RUN_PROFILE] == (PipelineStep.COMMIT_TURN,)
    assert not any(
        other in STEP_DEPENDENCIES[step] for step in BACKGROUND_STEPS for other in BACKGROUND_STEPS if other is not step
    )

    config = BackgroundStepConfig(run_midterm=True, run_longterm=False, run_profile=True)
    assert dependencies_for(PipelineStep.REFRESH_STATE, config) == (
        PipelineStep.RUN_MIDTERM,
        PipelineStep.RUN_PROFILE,
    )


def test_turn_background_configuration_is_persisted_per_turn_and_reopens(tmp_path):
    db_path = tmp_path / "demo.db"
    repository = DemoRepository(db_path)
    session = repository.create_session("simulation-1", "user-1", "run-1")
    first = _turn(
        repository,
        session,
        "first",
        config=BackgroundStepConfig(run_midterm=True, run_longterm=False, run_profile=True),
    )
    second = _turn(
        repository,
        session,
        "second",
        config=BackgroundStepConfig(run_midterm=False, run_longterm=True, run_profile=False),
    )

    reopened = DemoRepository(db_path)

    assert reopened.background_config(first["turn_id"]) == BackgroundStepConfig(True, False, True)
    assert reopened.background_config(second["turn_id"]) == BackgroundStepConfig(False, True, False)


def test_legacy_database_adds_default_configuration_and_migrates_run_migration(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE demo_sessions (
                session_id TEXT PRIMARY KEY,
                simulation_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (simulation_id, user_id, run_id)
            );
            CREATE TABLE demo_turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                user_message TEXT NOT NULL,
                assistant_message TEXT,
                generation_json TEXT,
                commit_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE demo_step_runs (
                turn_id TEXT NOT NULL REFERENCES demo_turns(turn_id) ON DELETE CASCADE,
                step TEXT NOT NULL,
                position INTEGER NOT NULL,
                status TEXT NOT NULL,
                input_json TEXT,
                output_json TEXT,
                error_type TEXT,
                error_message TEXT,
                started_at TEXT,
                ended_at TEXT,
                duration_ms REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                lock_token TEXT,
                lease_expires_at TEXT,
                before_snapshot_id TEXT,
                after_snapshot_id TEXT,
                diff_json TEXT,
                skip_reason TEXT,
                PRIMARY KEY (turn_id, step)
            );
            INSERT INTO demo_sessions VALUES ('session-1', 'legacy', 'user-1', 'run-1', 'now', 'now');
            INSERT INTO demo_turns VALUES (
                'turn-1', 'session-1', 'user-1', 'run-1', 'legacy message',
                NULL, NULL, NULL, 'now', 'now'
            );
            INSERT INTO demo_step_runs (turn_id, step, position, status, attempts)
            VALUES ('turn-1', 'run_migration', 5, 'succeeded', 1);
            """
        )

    repository = DemoRepository(db_path)
    steps = {step["step"]: step for step in repository.list_steps("turn-1")}

    assert repository.background_config("turn-1") == BackgroundStepConfig()
    assert steps[PipelineStep.RUN_MIDTERM.value]["status"] == "succeeded"
    assert steps[PipelineStep.RUN_LONGTERM.value]["status"] == "succeeded"
    assert "run_migration" not in steps
    assert len(steps) == 9
    assert len(DemoRepository(db_path).list_steps("turn-1")) == 9


def test_disabled_branch_stays_pending_and_does_not_block_refresh(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = DemoBackgroundCoordinator("simulation-1", repository)
    pipeline.coordinator = coordinator
    pipeline.update_background_config(
        turn["turn_id"],
        BackgroundStepConfig(run_midterm=True, run_longterm=False, run_profile=True),
        session_id=session["session_id"],
    )
    try:
        result = pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert result["background_submitted"]
        assert coordinator.wait_for_idle(2)

        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "succeeded"
        longterm = repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)
        assert longterm["status"] == "pending"
        assert longterm["attempts"] == 0
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_PROFILE)["status"] == "succeeded"
        assert memory.demo_background_worker.calls["longterm"] == []
        assert memory.demo_background_worker.jobs["migration-1"]["longterm_status"] == "pending"

        refreshed = pipeline.run_step(
            turn["turn_id"],
            PipelineStep.REFRESH_STATE,
            session_id=session["session_id"],
        )
        assert refreshed["status"] == "succeeded"
        assert pipeline_graph.progress(
            repository.list_steps(turn["turn_id"]),
            repository.background_config(turn["turn_id"]),
        )[:2] == (8, 8)
    finally:
        coordinator.shutdown(wait=True)


def test_next_and_run_all_submit_background_without_running_refresh_inline(tmp_path):
    pipeline, repository, _memory, session, turn = _pipeline(tmp_path / "next")
    coordinator = DemoBackgroundCoordinator("next", repository)
    pipeline.coordinator = coordinator
    try:
        for expected_step in (
            PipelineStep.CAPTURE_INPUT,
            PipelineStep.RETRIEVE_CONTEXT,
            PipelineStep.BUILD_PROMPT,
            PipelineStep.GENERATE_RESPONSE,
            PipelineStep.COMMIT_TURN,
        ):
            result = pipeline.run_next_step(turn["turn_id"], session_id=session["session_id"])
            assert result["step"] == expected_step.value

        submitted = pipeline.run_next_step(turn["turn_id"], session_id=session["session_id"])
        assert submitted["background_submitted"]
        assert repository.get_step(turn["turn_id"], PipelineStep.REFRESH_STATE)["status"] == "pending"
        assert coordinator.wait_for_idle(2)
        refreshed = pipeline.run_next_step(turn["turn_id"], session_id=session["session_id"])
        assert refreshed["step"] == PipelineStep.REFRESH_STATE.value

        second = pipeline.create_turn(
            session["session_id"],
            user_id=session["user_id"],
            run_id=session["run_id"],
            user_message="run all",
        )
        run_all = pipeline.run_all(second["turn_id"], session_id=session["session_id"])
        assert run_all["background_submitted"]
        assert repository.get_step(second["turn_id"], PipelineStep.COMMIT_TURN)["status"] == "succeeded"
        assert repository.get_step(second["turn_id"], PipelineStep.REFRESH_STATE)["status"] == "pending"
    finally:
        coordinator.shutdown(wait=True)


def test_three_background_queues_enter_concurrently_and_submission_returns(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("parallel", "user-1", "run-1")
    turn = _turn(repository, session, "parallel")
    coordinator = DemoBackgroundCoordinator("parallel", repository)
    barrier = threading.Barrier(3)
    release = threading.Event()
    entered = {step: threading.Event() for step in BACKGROUND_STEPS}

    def runner(turn_id, step, token, session_id):
        entered[step].set()
        barrier.wait(timeout=2)
        assert release.wait(2)
        _complete(repository, turn_id, step, token)

    try:
        results = coordinator.submit_enabled_branches(
            turn["turn_id"],
            session["session_id"],
            BACKGROUND_STEPS,
            runner,
        )
        assert all(result.submitted for result in results.values())
        assert all(event.wait(1) for event in entered.values())
        assert coordinator.is_running(turn["turn_id"])
    finally:
        release.set()
        assert coordinator.wait_for_idle(2)
        coordinator.shutdown(wait=True)


@pytest.mark.parametrize("step", BACKGROUND_STEPS)
def test_each_background_type_is_serial_across_turns(tmp_path, step):
    repository = DemoRepository(tmp_path / f"{step.value}.db")
    session = repository.create_session("serial", "user-1", "run-1")
    first = _turn(repository, session, "first")
    second = _turn(repository, session, "second")
    coordinator = DemoBackgroundCoordinator(f"serial-{step.value}", repository)
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    order = []

    def runner(turn_id, current_step, token, session_id):
        order.append(turn_id)
        if turn_id == first["turn_id"]:
            first_entered.set()
            assert release_first.wait(2)
        else:
            second_entered.set()
        _complete(repository, turn_id, current_step, token)

    try:
        assert coordinator.submit(first["turn_id"], step, session["session_id"], runner).submitted
        assert coordinator.submit(second["turn_id"], step, session["session_id"], runner).submitted
        assert first_entered.wait(1)
        assert not second_entered.wait(0.05)
        release_first.set()
        assert second_entered.wait(1)
        assert coordinator.wait_for_idle(2)
        assert order == [first["turn_id"], second["turn_id"]]
    finally:
        release_first.set()
        coordinator.shutdown(wait=True)


def test_duplicate_submission_and_repository_lease_execute_once(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("dedupe", "user-1", "run-1")
    turn = _turn(repository, session, "dedupe")
    first = DemoBackgroundCoordinator("dedupe-a", repository)
    second = DemoBackgroundCoordinator("dedupe-b", repository)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def runner(turn_id, step, token, session_id):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
        _complete(repository, turn_id, step, token)

    try:
        assert first.submit_midterm(turn["turn_id"], session["session_id"], runner).submitted
        assert entered.wait(1)
        duplicate = first.submit_midterm(turn["turn_id"], session["session_id"], runner)
        other_page = second.submit_midterm(turn["turn_id"], session["session_id"], runner)
        assert not duplicate.submitted
        assert not other_page.submitted
        assert {duplicate.reason, other_page.reason} == {"already_registered", "repository_lease"}
        release.set()
        assert first.wait_for_idle(2)
        completed = second.submit_midterm(turn["turn_id"], session["session_id"], runner)
        assert not completed.submitted
        assert completed.reason == "already_complete"
        assert calls == 1
    finally:
        release.set()
        first.shutdown(wait=True)
        second.shutdown(wait=True)


def test_branch_failure_does_not_cancel_others_and_retry_is_independent(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("failure", "user-1", "run-1")
    turn = _turn(repository, session, "failure")
    coordinator = DemoBackgroundCoordinator("failure", repository)
    barrier = threading.Barrier(3)
    calls = {step: 0 for step in BACKGROUND_STEPS}

    def runner(turn_id, step, token, session_id):
        calls[step] += 1
        barrier.wait(timeout=2)
        if step is PipelineStep.RUN_MIDTERM:
            raise RuntimeError("midterm failed")
        _complete(repository, turn_id, step, token)

    def retry_runner(turn_id, step, token, session_id):
        calls[step] += 1
        _complete(repository, turn_id, step, token)

    try:
        coordinator.submit_enabled_branches(
            turn["turn_id"],
            session["session_id"],
            BACKGROUND_STEPS,
            runner,
        )
        assert coordinator.wait_for_idle(2)
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "failed"
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)["status"] == "succeeded"
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_PROFILE)["status"] == "succeeded"

        retried = coordinator.submit_midterm(
            turn["turn_id"],
            session["session_id"],
            retry_runner,
            retry=True,
        )
        assert retried.submitted
        assert coordinator.wait_for_idle(2)
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "succeeded"
        assert calls == {
            PipelineStep.RUN_MIDTERM: 2,
            PipelineStep.RUN_LONGTERM: 1,
            PipelineStep.RUN_PROFILE: 1,
        }
    finally:
        coordinator.shutdown(wait=True)


def test_multiple_failed_steps_are_returned_and_only_selected_step_retries(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("multi-failure", "user-1", "run-1")
    turn = _turn(repository, session, "multi-failure")
    coordinator = DemoBackgroundCoordinator("multi-failure", repository)

    def fail_runner(turn_id, step, token, session_id):
        raise RuntimeError(f"{step.value} failed")

    def success_runner(turn_id, step, token, session_id):
        _complete(repository, turn_id, step, token)

    try:
        coordinator.submit_midterm(turn["turn_id"], session["session_id"], fail_runner)
        coordinator.submit_longterm(turn["turn_id"], session["session_id"], fail_runner)
        assert coordinator.wait_for_idle(2)
        pipeline = DemoPipelineService(object(), repository, object(), coordinator=coordinator)
        failed = pipeline.retryable_steps(turn["turn_id"], session_id=session["session_id"])
        assert {step["step"] for step in failed} == {
            PipelineStep.RUN_MIDTERM.value,
            PipelineStep.RUN_LONGTERM.value,
        }

        coordinator.submit_longterm(
            turn["turn_id"],
            session["session_id"],
            success_runner,
            retry=True,
        )
        assert coordinator.wait_for_idle(2)
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "failed"
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)["status"] == "succeeded"
    finally:
        coordinator.shutdown(wait=True)


def test_shutdown_rejects_new_work_waits_for_running_and_queued_tasks(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("lifecycle", "user-1", "run-1")
    first = _turn(repository, session, "first")
    second = _turn(repository, session, "second")
    coordinator = DemoBackgroundCoordinator("lifecycle", repository)
    entered = threading.Event()
    release = threading.Event()

    def runner(turn_id, step, token, session_id):
        entered.set()
        assert release.wait(2)
        _complete(repository, turn_id, step, token)

    coordinator.submit_midterm(first["turn_id"], session["session_id"], runner)
    coordinator.submit_midterm(second["turn_id"], session["session_id"], runner)
    assert entered.wait(1)
    shutdown = threading.Thread(target=coordinator.shutdown, kwargs={"wait": True})
    shutdown.start()
    with pytest.raises(RuntimeError, match="closed"):
        coordinator.submit_profile(first["turn_id"], session["session_id"], runner)
    assert shutdown.is_alive()
    release.set()
    shutdown.join(2)
    assert not shutdown.is_alive()
    assert not any(thread.name.startswith("demo-lifecycle-") for thread in threading.enumerate())

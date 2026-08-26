import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from memory_monitor.components import pipeline_graph, styles
from memory_monitor.models import (
    MEMORY_STEPS,
    PIPELINE_STEPS,
    STEP_DEPENDENCIES,
    BackgroundStepConfig,
    PipelineStep,
    StepStatus,
)
from memory_monitor.runtime import DemoBackgroundCoordinator, DemoMemory
from memory_monitor.services.demo_pipeline_service import DemoPipelineService, STEP_SNAPSHOT_SECTIONS
from memory_monitor.services.demo_repository import DemoRepository, StepAlreadyRunningError
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
    return repository.complete_step(
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


def test_demo_retrieval_warmup_is_once_under_concurrent_calls():
    memory = DemoMemory.__new__(DemoMemory)
    memory._demo_retrieval_warmup_lock = threading.Lock()
    memory._demo_retrieval_warmed_up = False
    calls = []
    entered = threading.Event()
    release = threading.Event()
    start = threading.Barrier(3)
    results = []

    def retrieve(query, *, user_id, session_id):
        calls.append((query, user_id, session_id))
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return {}

    def run_warmup():
        start.wait()
        results.append(memory.warm_up_retrieval_for_demo())

    memory.retrieve_context_for_demo = retrieve
    threads = [threading.Thread(target=run_warmup) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    assert entered.wait(1)
    release.set()
    for thread in threads:
        thread.join(2)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]
    assert calls == [
        ("金融分析预热", "__demo_warmup_user__", "__demo_warmup_session__"),
        ("retrieval warmup", "__demo_warmup_user__", "__demo_warmup_session__"),
    ]


def test_dag_has_four_visible_branches_and_derived_completion_only():
    assert PIPELINE_STEPS[:5] == (
        PipelineStep.CAPTURE_INPUT,
        PipelineStep.RETRIEVE_CONTEXT,
        PipelineStep.AGENTIC_RETRIEVAL,
        PipelineStep.BUILD_PROMPT,
        PipelineStep.GENERATE_RESPONSE,
    )
    assert STEP_DEPENDENCIES[PipelineStep.CAPTURE_INPUT] == ()
    assert STEP_DEPENDENCIES[PipelineStep.RETRIEVE_CONTEXT] == (PipelineStep.CAPTURE_INPUT,)
    assert STEP_DEPENDENCIES[PipelineStep.AGENTIC_RETRIEVAL] == (PipelineStep.RETRIEVE_CONTEXT,)
    assert STEP_DEPENDENCIES[PipelineStep.BUILD_PROMPT] == (PipelineStep.AGENTIC_RETRIEVAL,)
    assert STEP_DEPENDENCIES[PipelineStep.GENERATE_RESPONSE] == (PipelineStep.BUILD_PROMPT,)
    assert STEP_DEPENDENCIES[PipelineStep.RUN_SHORTTERM] == (PipelineStep.GENERATE_RESPONSE,)
    for step in MEMORY_STEPS[1:]:
        assert STEP_DEPENDENCIES[step] == (PipelineStep.RUN_SHORTTERM,)
    assert STEP_DEPENDENCIES[PipelineStep.COMPLETE_TURN] == MEMORY_STEPS
    assert PipelineStep.COMPLETE_TURN not in PIPELINE_STEPS

    steps = [{"step": step.value, "status": StepStatus.PENDING.value, "attempts": 0} for step in PIPELINE_STEPS]
    rendered = pipeline_graph.render_html(steps, BackgroundStepConfig())
    assert "提交当前轮" not in rendered
    assert "刷新状态" not in rendered
    assert "尝试 " not in rendered
    assert "0 次" in rendered
    assert rendered.count('class="demo-memory-branch"') == 4
    assert rendered.count("demo-fork-output") == 4
    assert rendered.count("demo-merge-input") == 4
    for center in ("12.5", "37.5", "62.5", "87.5"):
        assert f'y1="{center}"' in rendered
    assert "完成本轮" in rendered
    foreground_labels = ("捕获输入", "问题重写", "分层检索", "Agentic 检索", "构建 Prompt", "模型回答")
    assert [rendered.index(label) for label in foreground_labels] == sorted(
        rendered.index(label) for label in foreground_labels
    )
    ordered_classes = (
        "demo-foreground-chain",
        "demo-parallel-arrow",
        "demo-fork",
        "demo-memory-column",
        "demo-merge",
        "demo-complete-node",
    )
    offsets = [rendered.index(class_name) for class_name in ordered_classes]
    assert offsets == sorted(offsets)
    assert "display: flex" in styles._DEMO_LAB_CSS
    assert ".demo-memory-column" in styles._DEMO_LAB_CSS
    assert "flex-direction: column" in styles._DEMO_LAB_CSS
    pipeline_scroll = styles._DEMO_LAB_CSS.split(".demo-pipeline-scroll {", 1)[1].split("}", 1)[0]
    flow_row = styles._DEMO_LAB_CSS.split(".demo-flow-row {", 1)[1].split("}", 1)[0]
    memory_branch = styles._DEMO_LAB_CSS.split(".demo-memory-branch {", 1)[1].split("}", 1)[0]
    assert "overflow-x: hidden" in pipeline_scroll
    assert "min-height: 370px" in pipeline_scroll
    assert "width: 100%" in flow_row
    assert "min-width: 0" in flow_row
    assert "min-height: 350px" in flow_row
    assert "flex: 0 0 25%" in memory_branch
    assert "padding-block: 0.3rem" in memory_branch
    assert "box-sizing: border-box" in memory_branch
    assert "max-content" not in styles._DEMO_LAB_CSS
    assert 'class="demo-complete-node"' in rendered
    assert 'title="四个记忆步骤全部完成后，本轮自动完成"' in rendered
    assert "grid-template-columns: repeat(4" not in styles._DEMO_LAB_CSS
    assert "grid-template-columns: repeat(2" not in styles._DEMO_LAB_CSS
    assert ".demo-fork::before" not in styles._DEMO_LAB_CSS
    assert ".demo-fork::after" not in styles._DEMO_LAB_CSS
    assert ".demo-merge::before" not in styles._DEMO_LAB_CSS
    assert ".demo-merge::after" not in styles._DEMO_LAB_CSS
    assert ".demo-memory-branch::before" not in styles._DEMO_LAB_CSS
    assert ".demo-memory-branch::after" not in styles._DEMO_LAB_CSS
    assert "demo-stage-arrow" not in rendered
    assert ".demo-stage-arrow" not in styles._DEMO_LAB_CSS
    assert 'class="demo-parallel-arrow"' in rendered
    assert 'marker-end="url(#demo-parallel-arrowhead)"' in rendered
    parallel_arrow = styles._DEMO_LAB_CSS.split(".demo-parallel-arrow {", 1)[1].split("}", 1)[0]
    assert "flex: 0 0 20px" in parallel_arrow
    assert "width: 20px" in parallel_arrow
    assert "min-width: 20px" in parallel_arrow
    foreground = styles._DEMO_LAB_CSS.split(".demo-foreground-chain {", 1)[1].split("}", 1)[0]
    assert "flex: 0 1 auto" in foreground
    assert "flex: 1 1 auto" not in foreground
    assert "justify-content: center" in flow_row
    agentic = next(step for step in steps if step["step"] == PipelineStep.AGENTIC_RETRIEVAL.value)
    agentic.update(
        status=StepStatus.SUCCEEDED.value,
        attempts=1,
        duration_ms=6575,
        output={
            "agentic_status": "not_needed",
            "llm_calls": [{"sequence": 1}],
            "tool_calls": [],
        },
    )
    rendered = pipeline_graph.render_html(steps, BackgroundStepConfig())
    assert 'class="demo-node succeeded agentic-retrieval"' in rendered
    assert "succeeded · 无需补充" in rendered
    assert "1 次 · 6575 ms" in rendered
    assert "LLM ×1" in rendered
    assert "工具 ×0" in rendered
    assert rendered.count("无需补充") == 1
    assert '<div class="demo-node-detail">无需补充</div>' not in rendered
    agentic_node = styles._DEMO_LAB_CSS.split(
        ".demo-foreground-chain .demo-node.agentic-retrieval {",
        1,
    )[1].split("}", 1)[0]
    assert "flex: 0 0 136px" in agentic_node
    assert "width: 136px" in agentic_node
    assert "min-width: 136px" in agentic_node
    assert "max-width: 136px" in agentic_node
    assert "height:" not in agentic_node
    assert "padding:" not in agentic_node
    assert "width: clamp(92px, 7vw, 108px)" in styles._DEMO_LAB_CSS
    assert "@container (max-width: 780px)" in styles._DEMO_LAB_CSS
    assert "width: 768px" in styles._DEMO_LAB_CSS
    assert "min-width: 768px" in styles._DEMO_LAB_CSS
    for agentic_status, detail in (
        ("supplemented", "已补充"),
        ("no_relevant_memory", "未检索到相关记忆"),
        ("degraded", "降级"),
    ):
        agentic["output"]["agentic_status"] = agentic_status
        state_rendered = pipeline_graph.render_html(steps, BackgroundStepConfig())
        assert f"succeeded · {detail}" in state_rendered
        assert f'<div class="demo-node-detail">{detail}</div>' in state_rendered
    assert 'marker-end="url(#demo-fork-arrow)"' in rendered
    assert 'marker-end="url(#demo-merge-arrow)"' in rendered


def test_legacy_turn_configuration_creates_persisted_pending_holds(tmp_path):
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
    blocked = reopened.get_step(first["turn_id"], PipelineStep.RUN_LONGTERM)
    assert blocked["status"] == "pending"
    assert blocked["is_held"] is True
    assert blocked["attempts"] == 0
    assert reopened.get_step(second["turn_id"], PipelineStep.RUN_MIDTERM)["is_held"] is True
    assert reopened.get_step(second["turn_id"], PipelineStep.RUN_PROFILE)["is_held"] is True


def test_legacy_disabled_skip_migrates_only_unfinished_turn_to_pending_hold(tmp_path):
    db_path = tmp_path / "legacy-disabled.db"
    repository = DemoRepository(db_path)
    session = repository.create_session("simulation-1", "user-1", "run-1")
    active = _turn(repository, session, "active")
    historical = _turn(repository, session, "historical")
    with repository._connection() as connection:
        connection.execute(
            "UPDATE demo_turns SET run_longterm = 0 WHERE turn_id = ?",
            (active["turn_id"],),
        )
        connection.execute(
            "UPDATE demo_turns SET run_longterm = 0, completed_at = updated_at WHERE turn_id = ?",
            (historical["turn_id"],),
        )
        connection.executemany(
            """
            UPDATE demo_step_runs
            SET status = ?, skip_reason = ?, ended_at = '2024-01-01T00:00:00+00:00'
            WHERE turn_id = ? AND step = ?
            """,
            [
                (
                    StepStatus.SKIPPED.value,
                    "Disabled by turn configuration",
                    active["turn_id"],
                    PipelineStep.RUN_LONGTERM.value,
                ),
                (
                    StepStatus.SKIPPED.value,
                    "Disabled by turn configuration",
                    historical["turn_id"],
                    PipelineStep.RUN_LONGTERM.value,
                ),
            ],
        )
        connection.commit()

    reopened = DemoRepository(db_path)
    migrated = reopened.get_step(active["turn_id"], PipelineStep.RUN_LONGTERM)
    preserved = reopened.get_step(historical["turn_id"], PipelineStep.RUN_LONGTERM)
    assert (migrated["status"], migrated["is_held"], migrated["skip_reason"], migrated["ended_at"]) == (
        StepStatus.PENDING.value,
        True,
        None,
        None,
    )
    assert (
        preserved["status"],
        preserved["is_held"],
        preserved["skip_reason"],
        preserved["ended_at"],
    ) == (
        StepStatus.SKIPPED.value,
        False,
        "Disabled by turn configuration",
        "2024-01-01T00:00:00+00:00",
    )

    reopened_again = DemoRepository(db_path)
    assert reopened_again.get_step(active["turn_id"], PipelineStep.RUN_LONGTERM) == migrated


def test_lightweight_demo_schema_migration_adds_defaults_and_new_steps(tmp_path):
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

    assert repository.background_config("turn-1") == BackgroundStepConfig()
    assert [step["step"] for step in repository.list_steps("turn-1")] == [step.value for step in PIPELINE_STEPS]
    assert all(step["status"] == "pending" for step in repository.list_steps("turn-1"))
    with repository._connection() as connection:
        step_columns = {row["name"] for row in connection.execute("PRAGMA table_info(demo_step_runs)")}
        turn_columns = {row["name"] for row in connection.execute("PRAGMA table_info(demo_turns)")}
    assert "queued_at" in step_columns
    assert "is_held" in step_columns
    assert {"custom_prompt", "run_shortterm", "execution_target", "completed_at"} <= turn_columns
    assert repository.get_turn("turn-1")["custom_prompt"] is None


def test_run_all_returns_immediately_while_two_slow_turns_run_concurrently(tmp_path):
    pipeline, repository, memory, session, first = _pipeline(tmp_path)
    coordinator = DemoBackgroundCoordinator("slow", repository, foreground_workers=4)
    pipeline.coordinator = coordinator
    entered = 0
    entered_lock = threading.Lock()
    both_entered = threading.Event()
    release = threading.Event()

    def slow_generate(messages, **kwargs):
        nonlocal entered
        with entered_lock:
            entered += 1
            if entered == 2:
                both_entered.set()
        assert release.wait(2)
        return "slow answer"

    memory.generate_response_for_demo = slow_generate
    second = pipeline.create_turn(
        session["session_id"],
        user_id=session["user_id"],
        run_id=session["run_id"],
        user_message="second slow question",
    )
    try:
        started = time.perf_counter()
        pipeline.run_all(first["turn_id"], session_id=session["session_id"])
        first_elapsed = time.perf_counter() - started
        started = time.perf_counter()
        pipeline.run_all(second["turn_id"], session_id=session["session_id"])
        second_elapsed = time.perf_counter() - started

        assert first_elapsed < 0.2
        assert second_elapsed < 0.2
        assert both_entered.wait(2)
        assert len(repository.list_active_turns(session["session_id"])) == 2
    finally:
        release.set()
        assert coordinator.wait_for_idle(4)
        coordinator.shutdown(wait=True)


def test_queued_and_running_are_distinct_for_each_fifo_queue(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("queued", "user-1", "run-1")
    first = _turn(repository, session, "first")
    second = _turn(repository, session, "second")
    coordinator = DemoBackgroundCoordinator("queued", repository)
    entered = threading.Event()
    release = threading.Event()

    def runner(turn_id, step, token, session_id):
        entered.set()
        assert release.wait(2)
        _complete(repository, turn_id, step, token)

    try:
        coordinator.submit_midterm(first["turn_id"], session["session_id"], runner)
        coordinator.submit_midterm(second["turn_id"], session["session_id"], runner)
        assert entered.wait(1)
        first_step = repository.get_step(first["turn_id"], PipelineStep.RUN_MIDTERM)
        second_step = repository.get_step(second["turn_id"], PipelineStep.RUN_MIDTERM)
        assert first_step["status"] == "running"
        assert first_step["attempts"] == 1
        assert second_step["status"] == "queued"
        assert second_step["attempts"] == 0
    finally:
        release.set()
        assert coordinator.wait_for_idle(3)
        coordinator.shutdown(wait=True)


def test_queued_task_lease_is_heartbeated_while_waiting_in_fifo(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("queue-heartbeat", "user-1", "run-1")
    first = _turn(repository, session, "first")
    second = _turn(repository, session, "second")
    coordinator = DemoBackgroundCoordinator("queue-heartbeat", repository, lease_seconds=1)
    entered = threading.Event()
    release = threading.Event()

    def runner(turn_id, step, token, session_id):
        if turn_id == first["turn_id"]:
            entered.set()
            assert release.wait(3)
        _complete(repository, turn_id, step, token)

    try:
        coordinator.submit_midterm(first["turn_id"], session["session_id"], runner)
        coordinator.submit_midterm(second["turn_id"], session["session_id"], runner)
        assert entered.wait(1)
        time.sleep(1.2)
        repository.recover_expired_step_leases()
        assert repository.get_step(first["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "running"
        assert repository.get_step(second["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "queued"
    finally:
        release.set()
        assert coordinator.wait_for_idle(3)
        coordinator.shutdown(wait=True)


def test_four_memory_queues_can_enter_concurrently(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("parallel", "user-1", "run-1")
    turn = _turn(repository, session, "parallel")
    coordinator = DemoBackgroundCoordinator("parallel", repository)
    barrier = threading.Barrier(4)
    release = threading.Event()
    entered = {step: threading.Event() for step in MEMORY_STEPS}

    def runner(turn_id, step, token, session_id):
        entered[step].set()
        barrier.wait(timeout=2)
        assert release.wait(2)
        _complete(repository, turn_id, step, token)

    try:
        started = time.perf_counter()
        results = coordinator.submit_enabled_branches(
            turn["turn_id"],
            session["session_id"],
            MEMORY_STEPS,
            runner,
        )
        assert time.perf_counter() - started < 0.2
        assert all(result.submitted for result in results.values())
        assert all(event.wait(1) for event in entered.values())
        assert coordinator.is_running(turn["turn_id"])
    finally:
        release.set()
        assert coordinator.wait_for_idle(3)
        coordinator.shutdown(wait=True)


def test_shortterm_completion_fans_out_three_worker_branches_concurrently(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = DemoBackgroundCoordinator("batch-parallel", repository)
    pipeline.coordinator = coordinator
    entered = set()
    entered_lock = threading.Lock()
    all_entered = threading.Event()
    release = threading.Event()

    def delayed(name, processor):
        def wrapped(job_id):
            with entered_lock:
                entered.add(name)
                if entered == {"midterm", "longterm", "profile"}:
                    all_entered.set()
            assert release.wait(2)
            return processor(job_id)

        return wrapped

    worker = memory.demo_background_worker
    worker.process_midterm_job = delayed("midterm", worker.process_midterm_job)
    worker.process_longterm_job = delayed("longterm", worker.process_longterm_job)
    worker.process_profile_job = delayed("profile", worker.process_profile_job)
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert all_entered.wait(2)
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_SHORTTERM)["status"] == "succeeded"
        assert all(
            repository.get_step(turn["turn_id"], step)["status"] in {StepStatus.QUEUED.value, StepStatus.RUNNING.value}
            for step in MEMORY_STEPS[1:]
        )
    finally:
        release.set()
        assert coordinator.wait_for_idle(4)
        coordinator.shutdown(wait=True)

    assert memory.commit_calls == 1
    assert all(repository.get_step(turn["turn_id"], step)["status"] == "succeeded" for step in MEMORY_STEPS)
    for step in MEMORY_STEPS:
        assert set(repository.get_step(turn["turn_id"], step)["diff"]) == set(STEP_SNAPSHOT_SECTIONS[step])


@pytest.mark.parametrize("step", MEMORY_STEPS)
def test_each_memory_queue_is_fifo_across_turns(tmp_path, step):
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
        assert coordinator.wait_for_idle(3)
        assert order == [first["turn_id"], second["turn_id"]]
    finally:
        release_first.set()
        coordinator.shutdown(wait=True)


def test_released_branch_waits_for_shortterm_then_runs_from_persisted_target(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = DemoBackgroundCoordinator("release-before-dependency", repository)
    pipeline.coordinator = coordinator
    shortterm_entered = threading.Event()
    allow_shortterm = threading.Event()
    original_commit = memory.commit_demo_turn

    def blocked_commit(**kwargs):
        shortterm_entered.set()
        assert allow_shortterm.wait(2)
        return original_commit(**kwargs)

    memory.commit_demo_turn = blocked_commit
    pipeline.hold_step(turn["turn_id"], PipelineStep.RUN_MIDTERM, session_id=session["session_id"])
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert shortterm_entered.wait(2)

        released = pipeline.set_memory_step_runnable(
            turn["turn_id"],
            PipelineStep.RUN_MIDTERM,
            True,
            session_id=session["session_id"],
        )
        assert released["resumed"] is True
        assert released["submissions"] == {}
        waiting = repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)
        assert waiting["status"] == "pending"
        assert waiting["is_held"] is False
        assert repository.get_turn(turn["turn_id"])["execution_target"] == "all"

        allow_shortterm.set()
        assert coordinator.wait_for_idle(4)

        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "succeeded"
        assert memory.demo_background_worker.calls["midterm"] == ["migration-1"]
        assert memory.commit_calls == 1
    finally:
        allow_shortterm.set()
        coordinator.shutdown(wait=True)


def test_resuming_old_held_turn_does_not_interrupt_new_running_turn(tmp_path):
    pipeline, repository, memory, session, first = _pipeline(tmp_path)
    coordinator = DemoBackgroundCoordinator("multi-turn-held-resume", repository)
    pipeline.coordinator = coordinator
    original_commit = memory.commit_demo_turn
    original_profile = memory.demo_background_worker.process_profile_job
    original_complete_step = repository.complete_step
    second_profile_entered = threading.Event()
    allow_second_profile = threading.Event()
    first_midterm_resumed = threading.Event()

    def commit_with_turn_jobs(**kwargs):
        result = original_commit(**kwargs)
        if memory.commit_calls == 1:
            return result
        migration_id = f"migration-{memory.commit_calls}"
        profile_id = f"profile-{memory.commit_calls}"
        memory.demo_background_worker.jobs[migration_id] = {
            "job_id": migration_id,
            "status": "pending",
            "midterm_status": "pending",
            "longterm_status": "pending",
        }
        memory.demo_background_worker.jobs[profile_id] = {
            "job_id": profile_id,
            "status": "pending",
        }
        return {
            **result,
            "background": {
                "migration_job_id": migration_id,
                "profile_job_id": profile_id,
            },
        }

    def observe_completion(turn_id, step, token, **kwargs):
        result = original_complete_step(turn_id, step, token, **kwargs)
        if turn_id == first["turn_id"] and PipelineStep(step) is PipelineStep.RUN_MIDTERM:
            first_midterm_resumed.set()
        return result

    def block_second_profile(job_id):
        if job_id == "profile-2":
            second_profile_entered.set()
            assert allow_second_profile.wait(3)
        return original_profile(job_id)

    memory.commit_demo_turn = commit_with_turn_jobs
    memory.demo_background_worker.process_profile_job = block_second_profile
    repository.complete_step = observe_completion
    pipeline.hold_step(first["turn_id"], PipelineStep.RUN_MIDTERM, session_id=session["session_id"])
    try:
        pipeline.run_all(first["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(4)
        assert repository.get_step(first["turn_id"], PipelineStep.RUN_MIDTERM)["is_held"] is True

        second = pipeline.create_turn(
            session["session_id"],
            user_id=session["user_id"],
            run_id=session["run_id"],
            user_message="second turn",
        )
        pipeline.run_all(second["turn_id"], session_id=session["session_id"])
        assert second_profile_entered.wait(3)
        assert {turn["turn_id"] for turn in repository.list_active_turns(session["session_id"])} == {
            first["turn_id"],
            second["turn_id"],
        }

        pipeline.set_memory_step_runnable(
            first["turn_id"],
            PipelineStep.RUN_MIDTERM,
            True,
            session_id=session["session_id"],
        )
        assert first_midterm_resumed.wait(3)
        assert repository.get_turn(first["turn_id"])["completed_at"] is not None
        assert repository.get_step(second["turn_id"], PipelineStep.RUN_PROFILE)["status"] == "running"
        assert repository.get_turn(second["turn_id"])["completed_at"] is None
        assert memory.commit_calls == 2
        assert repository.get_step(first["turn_id"], PipelineStep.RUN_SHORTTERM)["attempts"] == 1
    finally:
        allow_second_profile.set()
        assert coordinator.wait_for_idle(4)
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
        assert first.wait_for_idle(3)
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
    barrier = threading.Barrier(4)
    calls = {step: 0 for step in MEMORY_STEPS}

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
            MEMORY_STEPS,
            runner,
        )
        assert coordinator.wait_for_idle(3)
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "failed"
        assert all(
            repository.get_step(turn["turn_id"], step)["status"] == "succeeded"
            for step in MEMORY_STEPS
            if step is not PipelineStep.RUN_MIDTERM
        )

        assert coordinator.submit_midterm(
            turn["turn_id"],
            session["session_id"],
            retry_runner,
            retry=True,
        ).submitted
        assert coordinator.wait_for_idle(3)
        assert calls[PipelineStep.RUN_MIDTERM] == 2
        assert all(calls[step] == 1 for step in MEMORY_STEPS if step is not PipelineStep.RUN_MIDTERM)
    finally:
        coordinator.shutdown(wait=True)


def test_multiple_failures_are_listed_and_only_the_selected_step_retries(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("multi-failure", "user-1", "run-1")
    turn = _turn(repository, session, "multi-failure")
    coordinator = DemoBackgroundCoordinator("multi-failure", repository)
    calls = {PipelineStep.RUN_MIDTERM: 0, PipelineStep.RUN_LONGTERM: 0}

    def fail(turn_id, step, token, session_id):
        calls[step] += 1
        raise RuntimeError(f"{step.value} failed")

    def succeed(turn_id, step, token, session_id):
        calls[step] += 1
        _complete(repository, turn_id, step, token)

    try:
        coordinator.submit_midterm(turn["turn_id"], session["session_id"], fail)
        coordinator.submit_longterm(turn["turn_id"], session["session_id"], fail)
        assert coordinator.wait_for_idle(3)
        pipeline = DemoPipelineService(object(), repository, object(), coordinator=coordinator)
        assert {
            step["step"] for step in pipeline.retryable_steps(turn["turn_id"], session_id=session["session_id"])
        } == {
            PipelineStep.RUN_MIDTERM.value,
            PipelineStep.RUN_LONGTERM.value,
        }

        coordinator.submit_longterm(
            turn["turn_id"],
            session["session_id"],
            succeed,
            retry=True,
        )
        assert coordinator.wait_for_idle(3)
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "failed"
        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)["status"] == "succeeded"
        assert calls == {PipelineStep.RUN_MIDTERM: 1, PipelineStep.RUN_LONGTERM: 2}
    finally:
        coordinator.shutdown(wait=True)


def test_old_execution_token_cannot_overwrite_a_new_retry(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("token", "user-1", "run-1")
    turn = _turn(repository, session, "token")
    old_token = repository.queue_step(turn["turn_id"], PipelineStep.CAPTURE_INPUT, lease_seconds=1)
    assert repository.start_step(turn["turn_id"], PipelineStep.CAPTURE_INPUT, old_token, lease_seconds=1)
    with repository._connection() as connection:
        connection.execute(
            """
            UPDATE demo_step_runs
            SET lease_expires_at = ?
            WHERE turn_id = ? AND step = ?
            """,
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                turn["turn_id"],
                PipelineStep.CAPTURE_INPUT.value,
            ),
        )
        connection.commit()
    repository.recover_expired_step_leases()
    new_token = repository.queue_step(
        turn["turn_id"],
        PipelineStep.CAPTURE_INPUT,
        lease_seconds=30,
        retry=True,
    )
    assert repository.start_step(turn["turn_id"], PipelineStep.CAPTURE_INPUT, new_token)
    _complete(repository, turn["turn_id"], PipelineStep.CAPTURE_INPUT, new_token)

    with pytest.raises(StepAlreadyRunningError, match="lease was lost"):
        _complete(repository, turn["turn_id"], PipelineStep.CAPTURE_INPUT, old_token)
    assert repository.get_step(turn["turn_id"], PipelineStep.CAPTURE_INPUT)["status"] == "succeeded"


def test_expired_queued_and_running_leases_recover_to_actionable_states(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("recovery", "user-1", "run-1")
    turn = _turn(repository, session, "recovery")
    queued_token = repository.queue_step(turn["turn_id"], PipelineStep.CAPTURE_INPUT)
    running_token = repository.queue_step(turn["turn_id"], PipelineStep.RETRIEVE_CONTEXT)
    assert repository.start_step(turn["turn_id"], PipelineStep.RETRIEVE_CONTEXT, running_token)
    with repository._connection() as connection:
        connection.execute(
            """
            UPDATE demo_step_runs SET lease_expires_at = ?
            WHERE turn_id = ? AND step IN (?, ?)
            """,
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                turn["turn_id"],
                PipelineStep.CAPTURE_INPUT.value,
                PipelineStep.RETRIEVE_CONTEXT.value,
            ),
        )
        connection.commit()

    repository.recover_expired_step_leases()

    queued = repository.get_step(turn["turn_id"], PipelineStep.CAPTURE_INPUT)
    running = repository.get_step(turn["turn_id"], PipelineStep.RETRIEVE_CONTEXT)
    assert queued["status"] == "pending"
    assert queued["lock_token"] is None
    assert running["status"] == "failed"
    assert running["lock_token"] is None
    assert queued_token


@pytest.mark.parametrize("start", [False, True], ids=["queued", "running"])
def test_reset_is_rejected_for_queued_or_running_work(tmp_path, start):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("no-reset", "user-1", "run-1")
    turn = _turn(repository, session, "no reset")
    token = repository.queue_step(turn["turn_id"], PipelineStep.CAPTURE_INPUT)
    if start:
        assert repository.start_step(turn["turn_id"], PipelineStep.CAPTURE_INPUT, token)

    with pytest.raises(RuntimeError, match="queued or running"):
        repository.reset_turn(turn["turn_id"])


def test_held_memory_step_stays_pending_across_restart_and_runs_immediately_after_release(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = DemoBackgroundCoordinator("held", repository)
    pipeline.coordinator = coordinator
    turn_id = turn["turn_id"]
    try:
        held = pipeline.hold_step(
            turn_id,
            PipelineStep.RUN_MIDTERM,
            session_id=session["session_id"],
        )
        assert held["status"] == StepStatus.PENDING.value
        assert held["is_held"] is True

        reopened = DemoRepository(repository.db_path)
        persisted = reopened.get_step(turn_id, PipelineStep.RUN_MIDTERM)
        assert persisted["status"] == StepStatus.PENDING.value
        assert persisted["is_held"] is True

        pipeline.run_all(turn_id, session_id=session["session_id"])
        assert coordinator.wait_for_idle(4)
        pipeline.run_remaining(turn_id, session_id=session["session_id"])
        assert coordinator.wait_for_idle(1)

        still_held = repository.get_step(turn_id, PipelineStep.RUN_MIDTERM)
        assert still_held["status"] == StepStatus.PENDING.value
        assert still_held["is_held"] is True
        assert memory.demo_background_worker.calls["midterm"] == []
        assert repository.get_turn(turn_id)["completed_at"] is None
        rendered = pipeline_graph.render_html(
            repository.list_steps(turn_id),
            repository.background_config(turn_id),
        )
        assert "pending · 已阻塞" in rendered
        assert "重新勾选后继续执行" in rendered

        pipeline.set_memory_step_runnable(
            turn_id,
            PipelineStep.RUN_MIDTERM,
            True,
            session_id=session["session_id"],
        )
        assert coordinator.wait_for_idle(4)
        released = repository.get_step(turn_id, PipelineStep.RUN_MIDTERM)
        assert released["status"] == StepStatus.SUCCEEDED.value
        assert released["is_held"] is False
        assert memory.demo_background_worker.calls["midterm"] == ["migration-1"]
        assert repository.get_turn(turn_id)["completed_at"] is not None
    finally:
        coordinator.shutdown(wait=True)


def test_restart_restores_held_step_and_persisted_target_before_release(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    first_coordinator = DemoBackgroundCoordinator("held-before-restart", repository)
    pipeline.coordinator = first_coordinator
    pipeline.hold_step(turn["turn_id"], PipelineStep.RUN_MIDTERM, session_id=session["session_id"])
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert first_coordinator.wait_for_idle(4)
    finally:
        first_coordinator.shutdown(wait=True)

    reopened = DemoRepository(repository.db_path)
    resumed_coordinator = DemoBackgroundCoordinator("held-after-restart", reopened)
    resumed = DemoPipelineService(
        memory,
        reopened,
        pipeline.state_service,
        coordinator=resumed_coordinator,
    )
    try:
        persisted_turn = reopened.get_turn(turn["turn_id"])
        persisted_step = reopened.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)
        assert persisted_turn["execution_target"] == "all"
        assert persisted_turn["completed_at"] is None
        assert (persisted_step["status"], persisted_step["is_held"]) == ("pending", True)

        resumed.resume_pending_work()
        assert resumed_coordinator.wait_for_idle(1)
        assert reopened.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["attempts"] == 0

        resumed.set_memory_step_runnable(
            turn["turn_id"],
            PipelineStep.RUN_MIDTERM,
            True,
            session_id=session["session_id"],
        )
        assert resumed_coordinator.wait_for_idle(4)
        assert reopened.get_step(turn["turn_id"], PipelineStep.RUN_MIDTERM)["status"] == "succeeded"
        assert reopened.get_turn(turn["turn_id"])["completed_at"] is not None
        assert memory.commit_calls == 1
    finally:
        resumed_coordinator.shutdown(wait=True)


@pytest.mark.parametrize(
    "step,status",
    [
        (PipelineStep.CAPTURE_INPUT, StepStatus.PENDING.value),
        (PipelineStep.RUN_SHORTTERM, StepStatus.QUEUED.value),
        (PipelineStep.RUN_MIDTERM, StepStatus.FAILED.value),
        (PipelineStep.RUN_LONGTERM, StepStatus.SUCCEEDED.value),
        (PipelineStep.RUN_PROFILE, StepStatus.SKIPPED.value),
    ],
)
def test_only_pending_memory_steps_can_be_held(tmp_path, step, status):
    repository = DemoRepository(tmp_path / f"{step.value}-{status}.db")
    session = repository.create_session("hold-validation", "user-1", "run-1")
    turn = _turn(repository, session, "validation")
    with repository._connection() as connection:
        connection.execute(
            "UPDATE demo_step_runs SET status = ? WHERE turn_id = ? AND step = ?",
            (status, turn["turn_id"], step.value),
        )
        connection.commit()

    with pytest.raises(ValueError, match="pending memory"):
        repository.hold_step(turn["turn_id"], step)


def test_held_core_branch_is_not_called_and_prevents_completion_until_release(tmp_path):
    pipeline, repository, memory, session, turn = _pipeline(tmp_path)
    coordinator = DemoBackgroundCoordinator("disabled", repository)
    pipeline.coordinator = coordinator
    pipeline.update_background_config(
        turn["turn_id"],
        BackgroundStepConfig(run_midterm=True, run_longterm=False, run_profile=True),
        session_id=session["session_id"],
    )
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(4)

        longterm = repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)
        assert longterm["status"] == "pending"
        assert longterm["is_held"] is True
        assert longterm["attempts"] == 0
        assert memory.demo_background_worker.calls["longterm"] == []
        assert memory.demo_background_worker.jobs["migration-1"]["longterm_status"] == "pending"
        assert repository.get_turn(turn["turn_id"])["completed_at"] is None
        assert repository.get_turn(turn["turn_id"])["execution_target"] == "all"
        assert [item["turn_id"] for item in repository.list_active_turns(session["session_id"])] == [turn["turn_id"]]

        pipeline.set_memory_step_runnable(
            turn["turn_id"],
            PipelineStep.RUN_LONGTERM,
            True,
            session_id=session["session_id"],
        )
        assert coordinator.wait_for_idle(4)

        assert repository.get_step(turn["turn_id"], PipelineStep.RUN_LONGTERM)["status"] == "succeeded"
        assert memory.demo_background_worker.calls["longterm"] == ["migration-1"]
        assert repository.get_turn(turn["turn_id"])["completed_at"] is not None
        assert repository.list_active_turns(session["session_id"]) == []
        assert repository.list_turns(session["session_id"])[0]["turn_id"] == turn["turn_id"]
        assert pipeline_graph.progress(
            repository.list_steps(turn["turn_id"]),
            repository.background_config(turn["turn_id"]),
        )[:2] == (10, 10)
    finally:
        coordinator.shutdown(wait=True)


def test_shutdown_rejects_new_work_and_waits_for_running_and_queued_tasks(tmp_path):
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
    shutdown.join(3)
    assert not shutdown.is_alive()
    assert not any(thread.name.startswith("demo-lifecycle-") for thread in threading.enumerate())

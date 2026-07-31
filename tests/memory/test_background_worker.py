import threading
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.base import BackgroundTaskConfig, MemoryConfig, UserProfileConfig
from mem0.memory import main as memory_main
from mem0.memory.background_worker import BackgroundWorkerManager, LeaseHeartbeat
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.profile_manager import ProfileManager
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.storage import SQLiteManager
from mem0.utils.timestamps import beijing_now


def _messages(value):
    return [
        {"role": "user", "content": f"user-{value}"},
        {"role": "assistant", "content": f"assistant-{value}"},
    ]


def _reserve(db, value, *, scope="user_id=u1&run_id=r1", max_messages=0):
    job_id = db.save_messages_and_create_migration_job(
        _messages(value),
        scope,
        max_messages=max_messages,
        filters={"user_id": "u1", "run_id": scope},
        metadata={"user_id": "u1", "run_id": scope},
        infer=True,
        prompt=None,
    )
    assert job_id
    return job_id


def _manager(db, *, config=None, midterm=None, longterm=None, profile=None):
    config = config or BackgroundTaskConfig(poll_interval_seconds=0.01)
    return BackgroundWorkerManager(
        db,
        config,
        process_midterm=midterm or (lambda *args: None),
        process_longterm=longterm or (lambda *args: None),
        process_profile=profile or (lambda *args: None),
    )


@pytest.fixture
def db():
    manager = SQLiteManager(":memory:")
    yield manager
    if manager.connection:
        manager.close()


def test_background_config_defaults():
    config = MemoryConfig().background

    assert config.enabled is True
    assert config.max_retries == 3
    assert config.retry_delays_seconds == (2.0, 10.0, 30.0)
    assert config.poll_interval_seconds == 1.0
    assert config.lease_timeout_seconds == 120.0
    assert config.heartbeat_interval_seconds == 20.0
    assert config.watchdog_interval_seconds == 10.0
    assert config.max_stale_recoveries == 3
    assert config.shutdown_timeout_seconds == 30.0
    assert config.include_pending_in_context is True
    assert config.max_pending_context_messages == 20
    assert config.include_failed_in_context is False


def test_start_creates_three_named_workers_and_is_idempotent(db):
    manager = _manager(db)
    try:
        manager.start()
        first_threads = list(manager._threads)
        manager.start()

        assert manager._threads == first_threads
        assert {thread.name for thread in first_threads} == {
            "mem0-midterm-memory-worker",
            "mem0-longterm-memory-worker",
            "mem0-profile-update-worker",
        }
        assert all(thread.is_alive() for thread in first_threads)
    finally:
        assert manager.stop(timeout=1)
        assert manager.stop(timeout=1)


def test_claim_assigns_unique_lease_token_and_stale_attempt_is_fenced(db):
    job_id = _reserve(db, "lease-fencing")
    first = db.claim_migration_stage(job_id, "midterm", lease_timeout_seconds=1)
    assert first["midterm_lease_token"]
    db.connection.execute(
        "UPDATE memory_migration_jobs SET midterm_lease_expires_at = ? WHERE job_id = ?",
        ((beijing_now() - timedelta(seconds=1)).isoformat(), job_id),
    )
    db.connection.commit()

    assert db.recover_expired_background_leases(max_stale_recoveries=3)["midterm"] == 1
    second = db.claim_migration_stage(job_id, "midterm", lease_timeout_seconds=1)
    assert second["midterm_lease_token"] != first["midterm_lease_token"]
    assert not db.mark_migration_stage_succeeded(
        job_id,
        "midterm",
        first["midterm_lease_token"],
    )
    assert (
        db.record_migration_stage_failure(
            job_id,
            "midterm",
            first["midterm_lease_token"],
            "late failure",
            max_retries=1,
            retry_delay_seconds=0,
        )
        == "stale_lease"
    )
    persisted = db.get_background_job(job_id)
    assert persisted["midterm_status"] == "running"
    assert persisted["midterm_lease_token"] == second["midterm_lease_token"]


def test_heartbeat_extends_lease(db):
    job_id = _reserve(db, "heartbeat")
    job = db.claim_migration_stage(job_id, "longterm", lease_timeout_seconds=0.05)
    previous_expiry = job["longterm_lease_expires_at"]
    time.sleep(0.01)

    assert db.heartbeat_migration_stage(
        job_id,
        "longterm",
        job["longterm_lease_token"],
        0.2,
    )
    persisted = db.get_background_job(job_id)
    assert persisted["longterm_heartbeat_at"] > job["longterm_heartbeat_at"]
    assert persisted["longterm_lease_expires_at"] > previous_expiry


def test_expired_migration_lease_is_immediately_invalid(db):
    job_id = _reserve(db, "expired-migration")
    job = db.claim_migration_stage(job_id, "midterm", lease_timeout_seconds=1)
    expired_at = (beijing_now() - timedelta(seconds=1)).isoformat()
    db.connection.execute(
        "UPDATE memory_migration_jobs SET midterm_lease_expires_at = ? WHERE job_id = ?",
        (expired_at, job_id),
    )
    db.connection.commit()
    token = job["midterm_lease_token"]

    assert not db.migration_stage_lease_is_current(job_id, "midterm", token)
    assert not db.heartbeat_migration_stage(job_id, "midterm", token, 10)
    assert not db.mark_migration_stage_succeeded(job_id, "midterm", token)
    assert (
        db.record_migration_stage_failure(
            job_id,
            "midterm",
            token,
            "late failure",
            max_retries=1,
            retry_delay_seconds=0,
        )
        == "stale_lease"
    )
    assert not db.mark_migration_stage_discarded(job_id, "midterm", token, "late discard")
    persisted = db.get_background_job(job_id)
    assert persisted["midterm_status"] == "running"
    assert persisted["midterm_attempts"] == 0
    assert persisted["midterm_last_error"] is None
    assert persisted["midterm_lease_expires_at"] == expired_at


def test_expired_profile_lease_is_immediately_invalid(db):
    job_id = db.create_profile_update_job("expired-profile", _messages("profile"))
    job = db.claim_profile_job(job_id, lease_timeout_seconds=1)
    expired_at = (beijing_now() - timedelta(seconds=1)).isoformat()
    db.connection.execute(
        "UPDATE profile_update_jobs SET lease_expires_at = ? WHERE job_id = ?",
        (expired_at, job_id),
    )
    db.connection.commit()
    token = job["lease_token"]

    assert not db.profile_job_lease_is_current(job_id, token)
    assert not db.heartbeat_profile_job(job_id, token, 10)
    assert not db.finish_profile_job(job_id, token)
    assert (
        db.record_profile_failure(
            job_id,
            token,
            "late failure",
            max_retries=1,
            retry_delay_seconds=0,
        )
        == "stale_lease"
    )
    persisted = db.get_background_job(job_id, "profile")
    assert persisted["status"] == "running"
    assert persisted["attempts"] == 0
    assert persisted["last_error"] is None
    assert persisted["lease_expires_at"] == expired_at


def test_profile_plan_and_finish_job_is_atomic_and_fenced(db, monkeypatch):
    plan = ProfileUpdatePlan.model_validate(
        {"operations": [{"operation": "set", "attribute_key": "analysis_role", "value": "fp_and_a"}]}
    )
    job_id = db.create_profile_update_job("atomic-profile", _messages("profile"))
    job = db.claim_profile_job(job_id, lease_timeout_seconds=5)

    assert db.apply_profile_plan_and_finish_job(
        job_id,
        job["lease_token"],
        "atomic-profile",
        plan,
    )
    assert db.get_background_job(job_id, "profile")["status"] == "succeeded"
    assert db.get_user_profile_values("atomic-profile")[0]["value"] == "fp_and_a"

    rollback_job_id = db.create_profile_update_job("rollback-profile", _messages("rollback"))
    rollback_job = db.claim_profile_job(rollback_job_id, lease_timeout_seconds=5)
    original_apply = db._apply_profile_update_plan_locked

    def fail_after_apply(*args, **kwargs):
        original_apply(*args, **kwargs)
        raise RuntimeError("forced SQL transaction failure")

    monkeypatch.setattr(db, "_apply_profile_update_plan_locked", fail_after_apply)
    with pytest.raises(RuntimeError, match="forced SQL transaction failure"):
        db.apply_profile_plan_and_finish_job(
            rollback_job_id,
            rollback_job["lease_token"],
            "rollback-profile",
            plan,
        )
    assert db.get_user_profile_values("rollback-profile") == []
    assert db.get_background_job(rollback_job_id, "profile")["status"] == "running"


def test_stale_or_expired_profile_job_cannot_apply_plan(db):
    plan = ProfileUpdatePlan.model_validate(
        {"operations": [{"operation": "set", "attribute_key": "analysis_role", "value": "fp_and_a"}]}
    )
    job_id = db.create_profile_update_job("stale-profile", _messages("profile"))
    first = db.claim_profile_job(job_id, lease_timeout_seconds=1)
    db.connection.execute(
        "UPDATE profile_update_jobs SET lease_expires_at = ? WHERE job_id = ?",
        ((beijing_now() - timedelta(seconds=1)).isoformat(), job_id),
    )
    db.connection.commit()

    assert not db.apply_profile_plan_and_finish_job(
        job_id,
        first["lease_token"],
        "stale-profile",
        plan,
    )
    assert db.get_user_profile_values("stale-profile") == []
    assert db.recover_expired_background_leases(max_stale_recoveries=3)["profile"] == 1
    second = db.claim_profile_job(job_id, lease_timeout_seconds=5)
    assert second["lease_token"] != first["lease_token"]
    assert not db.apply_profile_plan_and_finish_job(
        job_id,
        first["lease_token"],
        "stale-profile",
        plan,
    )
    assert db.get_user_profile_values("stale-profile") == []


def test_heartbeat_stop_respects_timeout():
    entered = threading.Event()
    release = threading.Event()

    def blocked_heartbeat():
        entered.set()
        release.wait(1)
        return True

    heartbeat = LeaseHeartbeat(
        heartbeat=blocked_heartbeat,
        interval_seconds=0.001,
        name="test-blocked-heartbeat",
    )
    heartbeat.start()
    assert entered.wait(1)
    started = time.monotonic()
    heartbeat.stop(timeout=0.02)
    assert time.monotonic() - started < 0.2
    assert heartbeat.is_alive()
    release.set()
    heartbeat.join(1)
    assert not heartbeat.is_alive()


def test_expired_lease_is_recovered_during_runtime(db):
    job_id = _reserve(db, "runtime-recovery")
    assert db.claim_migration_stage(job_id, "midterm", lease_timeout_seconds=0.03)
    processed = threading.Event()
    manager = _manager(
        db,
        config=BackgroundTaskConfig(
            poll_interval_seconds=0.005,
            lease_timeout_seconds=0.08,
            heartbeat_interval_seconds=0.01,
            watchdog_interval_seconds=0.01,
        ),
        midterm=lambda *args: processed.set(),
    )
    try:
        manager.start()
        assert processed.wait(1)
        assert manager.flush(2)
        job = db.get_background_job(job_id)
        assert job["midterm_status"] == "succeeded"
        assert job["midterm_recovery_count"] == 1
    finally:
        assert manager.stop(timeout=1)


def test_healthy_heartbeat_job_is_not_recovered(db):
    job_id = _reserve(db, "healthy-heartbeat")
    entered = threading.Event()
    release = threading.Event()

    def midterm(*args):
        entered.set()
        assert release.wait(1)

    manager = _manager(
        db,
        config=BackgroundTaskConfig(
            poll_interval_seconds=0.005,
            lease_timeout_seconds=0.06,
            heartbeat_interval_seconds=0.01,
            watchdog_interval_seconds=0.01,
        ),
        midterm=midterm,
    )
    try:
        manager.start()
        manager.wake_midterm()
        assert entered.wait(1)
        time.sleep(0.12)
        job = db.get_background_job(job_id)
        assert job["midterm_status"] == "running"
        assert job["midterm_recovery_count"] == 0
        release.set()
        assert manager.flush(2)
    finally:
        release.set()
        assert manager.stop(timeout=1)


def test_recovery_exhaustion_forces_degraded_stage(db):
    job_id = _reserve(db, "force-degraded")
    first = db.claim_migration_stage(job_id, "longterm", lease_timeout_seconds=1)
    db.connection.execute(
        "UPDATE memory_migration_jobs SET longterm_lease_expires_at = ? WHERE job_id = ?",
        ((beijing_now() - timedelta(seconds=1)).isoformat(), job_id),
    )
    db.connection.commit()
    assert db.recover_expired_background_leases(max_stale_recoveries=0)["longterm"] == 1
    recovered = db.claim_migration_stage(job_id, "longterm")
    assert recovered["longterm_force_degraded"] == 1
    assert recovered["longterm_lease_token"] != first["longterm_lease_token"]
    calls = []
    manager = _manager(db, longterm=lambda job, messages, degraded: calls.append(degraded))

    assert manager._run_migration_stage(recovered, "longterm", manager.process_longterm)
    assert calls == [True]
    assert db.get_background_job(job_id)["longterm_status"] == "succeeded_degraded"


def test_profile_recovery_exhaustion_discards_job(db):
    job_id = db.create_profile_update_job("profile-recovery", _messages("profile"))
    claimed = db.claim_profile_job(job_id)
    db.connection.execute(
        "UPDATE profile_update_jobs SET lease_expires_at = ? WHERE job_id = ?",
        ((beijing_now() - timedelta(seconds=1)).isoformat(), job_id),
    )
    db.connection.commit()

    assert db.recover_expired_background_leases(max_stale_recoveries=0)["profile"] == 1
    job = db.get_background_job(job_id, "profile")
    assert job["status"] == "discarded"
    assert job["recovery_count"] == 1
    assert job["lease_token"] is None
    assert claimed["lease_token"]


def test_watchdog_and_heartbeat_threads_stop_cleanly(db):
    job_id = _reserve(db, "thread-cleanup")
    entered = threading.Event()
    release = threading.Event()

    def midterm(*args):
        entered.set()
        release.wait(1)

    manager = _manager(
        db,
        config=BackgroundTaskConfig(
            poll_interval_seconds=0.005,
            lease_timeout_seconds=0.1,
            heartbeat_interval_seconds=0.01,
            watchdog_interval_seconds=0.01,
        ),
        midterm=midterm,
    )
    manager.start()
    manager.wake_midterm()
    assert entered.wait(1)
    assert any(
        thread.name.startswith("mem0-midterm-heartbeat-") and thread.is_alive()
        for thread in threading.enumerate()
    )
    release.set()
    assert manager.flush(2)
    assert manager.stop(timeout=1)
    assert not manager.threads_alive()
    assert not any(
        thread.name == "mem0-background-watchdog"
        or thread.name.startswith("mem0-midterm-heartbeat-")
        for thread in threading.enumerate()
    )
    assert db.get_background_job(job_id)["midterm_status"] == "succeeded"


def test_midterm_and_longterm_enter_same_job_in_parallel(db):
    job_id = _reserve(db, "parallel")
    midterm_entered = threading.Event()
    longterm_entered = threading.Event()
    release = threading.Event()

    def midterm(job, messages, degraded):
        midterm_entered.set()
        assert release.wait(2)

    def longterm(job, messages, degraded):
        longterm_entered.set()
        assert release.wait(2)

    manager = _manager(db, midterm=midterm, longterm=longterm)
    try:
        manager.start()
        manager.wake_migration()
        assert midterm_entered.wait(1)
        assert longterm_entered.wait(1)
        job = db.get_background_job(job_id)
        assert job["midterm_status"] == "running"
        assert job["longterm_status"] == "running"
        assert db.get_migration_job_messages(job_id)
        release.set()
        assert manager.flush(2)
    finally:
        release.set()
        assert manager.stop(timeout=1)


def test_different_sessions_can_run_in_parallel(db):
    midterm_job_id = _reserve(db, "session-midterm", scope="user_id=u1&run_id=midterm")
    longterm_job_id = _reserve(db, "session-longterm", scope="user_id=u1&run_id=longterm")
    db.connection.execute(
        "UPDATE memory_migration_jobs SET longterm_status = 'succeeded' WHERE job_id = ?",
        (midterm_job_id,),
    )
    db.connection.execute(
        "UPDATE memory_migration_jobs SET midterm_status = 'succeeded' WHERE job_id = ?",
        (longterm_job_id,),
    )
    db.connection.commit()
    midterm_entered = threading.Event()
    longterm_entered = threading.Event()
    release = threading.Event()

    def midterm(job, messages, degraded):
        assert job["job_id"] == midterm_job_id
        midterm_entered.set()
        assert release.wait(2)

    def longterm(job, messages, degraded):
        assert job["job_id"] == longterm_job_id
        longterm_entered.set()
        assert release.wait(2)

    manager = _manager(db, midterm=midterm, longterm=longterm)
    try:
        manager.start()
        manager.wake_migration()
        assert midterm_entered.wait(1)
        assert longterm_entered.wait(1)
        release.set()
        assert manager.flush(2)
    finally:
        release.set()
        assert manager.stop(timeout=1)


def test_shared_thread_safe_handler_can_overlap_across_all_workers(db):
    _reserve(db, "shared-provider")
    db.create_profile_update_job("shared-provider", _messages("shared-provider"))
    barrier = threading.Barrier(4)
    entered = []
    entered_lock = threading.Lock()

    def shared_call(name):
        with entered_lock:
            entered.append(name)
        barrier.wait(timeout=2)

    manager = _manager(
        db,
        midterm=lambda job, messages, degraded: shared_call("midterm"),
        longterm=lambda job, messages, degraded: shared_call("longterm"),
        profile=lambda job: shared_call("profile"),
    )
    try:
        manager.start()
        manager.wake_all()
        barrier.wait(timeout=2)
        assert set(entered) == {"midterm", "longterm", "profile"}
        assert manager.flush(2)
    finally:
        assert manager.stop(timeout=1)


def test_longterm_can_finish_while_midterm_runs(db):
    job_id = _reserve(db, "longterm-first")
    midterm_started = threading.Event()
    longterm_finished = threading.Event()
    release_midterm = threading.Event()

    def midterm(job, messages, degraded):
        midterm_started.set()
        assert release_midterm.wait(2)

    def longterm(job, messages, degraded):
        longterm_finished.set()

    manager = _manager(db, midterm=midterm, longterm=longterm)
    try:
        manager.start()
        manager.wake_migration()
        assert midterm_started.wait(1)
        assert longterm_finished.wait(1)
        deadline = time.monotonic() + 1
        while db.get_background_job(job_id)["longterm_status"] != "succeeded" and time.monotonic() < deadline:
            time.sleep(0.005)
        job = db.get_background_job(job_id)
        assert job["midterm_status"] == "running"
        assert job["longterm_status"] == "succeeded"
        assert db.get_migration_job_messages(job_id)
        release_midterm.set()
        assert manager.flush(2)
        assert db.get_migration_job_messages(job_id) == []
    finally:
        release_midterm.set()
        assert manager.stop(timeout=1)


def test_same_stage_cannot_be_claimed_twice(db):
    job_id = _reserve(db, "claim-once")
    barrier = threading.Barrier(3)
    claims = []

    def claim():
        barrier.wait(timeout=1)
        claims.append(db.claim_migration_stage(job_id, "midterm"))

    threads = [threading.Thread(target=claim), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=1)
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(isinstance(claimed, dict) for claimed in claims) == 1


def test_both_stages_can_claim_same_parent(db):
    job_id = _reserve(db, "dual-claim")
    barrier = threading.Barrier(3)
    claims = {}

    def claim(stage):
        barrier.wait(timeout=1)
        claims[stage] = db.claim_migration_stage(job_id, stage)

    threads = [
        threading.Thread(target=claim, args=("midterm",)),
        threading.Thread(target=claim, args=("longterm",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=1)
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert claims["midterm"]["midterm_status"] == "running"
    assert claims["longterm"]["longterm_status"] == "running"


def test_concurrent_finalization_deletes_messages_once(db):
    job_id = _reserve(db, "finalize")
    db.connection.execute(
        """
        UPDATE memory_migration_jobs
        SET midterm_status = 'succeeded', longterm_status = 'succeeded'
        WHERE job_id = ?
        """,
        (job_id,),
    )
    db.connection.commit()
    statements = []
    db.connection.set_trace_callback(statements.append)
    barrier = threading.Barrier(3)
    results = []

    def finalize():
        barrier.wait(timeout=1)
        results.append(db.finalize_migration_job_if_ready(job_id))

    threads = [threading.Thread(target=finalize), threading.Thread(target=finalize)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=1)
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]
    assert sum(statement.startswith("DELETE FROM messages") for statement in statements) == 1
    assert db.get_migration_job_messages(job_id) == []
    assert db.get_background_job(job_id)["status"] == "succeeded"
    db.connection.set_trace_callback(None)


@pytest.mark.parametrize(
    ("midterm_status", "longterm_status", "expected"),
    [
        ("pending", "pending", "pending"),
        ("succeeded", "pending", "pending"),
        ("retry", "pending", "retry"),
        ("retry", "running", "running"),
        ("discarded", "running", "running"),
        ("discarded", "retry", "retry"),
        ("succeeded", "succeeded", "succeeded"),
        ("succeeded_degraded", "succeeded", "succeeded_degraded"),
        ("discarded", "succeeded", "completed_with_loss"),
        ("discarded", "succeeded_degraded", "completed_with_loss"),
        ("discarded", "discarded", "completed_with_loss"),
    ],
)
def test_migration_parent_status_rules(midterm_status, longterm_status, expected):
    assert SQLiteManager._migration_parent_status(midterm_status, longterm_status) == expected


def test_reservation_keeps_source_messages_and_only_counts_active_rows(db):
    first_job = _reserve(db, 1)
    second_job = _reserve(db, 2)

    assert first_job != second_job
    assert [item["content"] for item in db.get_migration_job_messages(first_job)] == [
        "user-1",
        "assistant-1",
    ]
    assert db.get_messages("user_id=u1&run_id=r1") == []
    context = db.get_context_messages(
        "user_id=u1&run_id=r1",
        active_limit=10,
        include_pending=True,
        max_pending=2,
    )
    assert [item["content"] for item in context] == [
        "user-1",
        "assistant-1",
        "user-2",
        "assistant-2",
    ]
    assert db.get_background_job(first_job)["status"] == "pending"
    assert db.get_background_job(second_job)["sequence_no"] == 2


def test_midterm_can_finish_while_longterm_runs_and_deletes_only_after_both(db):
    job_id = _reserve(db, 1)
    longterm_started = threading.Event()
    release_longterm = threading.Event()
    midterm_finished = threading.Event()

    def midterm(job, messages, degraded):
        midterm_finished.set()

    def longterm(job, messages, degraded):
        longterm_started.set()
        assert db.get_migration_job_messages(job_id)
        release_longterm.wait(2)

    manager = _manager(db, midterm=midterm, longterm=longterm)
    manager.start()
    manager.wake_migration()
    assert longterm_started.wait(1)
    assert midterm_finished.wait(1)

    deadline = time.monotonic() + 1
    running = db.get_background_job(job_id)
    while running["midterm_status"] != "succeeded" and time.monotonic() < deadline:
        time.sleep(0.005)
        running = db.get_background_job(job_id)
    assert running["midterm_status"] == "succeeded"
    assert running["longterm_status"] == "running"
    assert db.get_migration_job_messages(job_id)
    context = db.get_context_messages(
        "user_id=u1&run_id=r1",
        active_limit=0,
        include_pending=False,
        max_pending=0,
    )
    assert [message["content"] for message in context] == ["user-1", "assistant-1"]

    release_longterm.set()
    assert manager.flush(2)
    assert db.get_background_job(job_id)["status"] == "succeeded"
    assert db.get_migration_job_messages(job_id) == []
    assert manager.stop(timeout=1)


def test_longterm_retry_keeps_messages_and_does_not_repeat_completed_midterm(db):
    job_id = _reserve(db, 1)
    midterm_calls = []
    longterm_calls = []

    def midterm(job, messages, degraded):
        midterm_calls.append(job["job_id"])

    def longterm(job, messages, degraded):
        longterm_calls.append(degraded)
        if len(longterm_calls) == 1:
            raise RuntimeError("temporary longterm failure")

    manager = _manager(
        db,
        config=BackgroundTaskConfig(
            max_retries=1,
            retry_delays_seconds=(0.05,),
            poll_interval_seconds=0.01,
        ),
        midterm=midterm,
        longterm=longterm,
    )
    manager.start()
    manager.wake_migration()

    deadline = time.monotonic() + 1
    while db.get_background_job(job_id)["longterm_status"] != "retry" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert db.get_background_job(job_id)["longterm_status"] == "retry"
    assert db.get_migration_job_messages(job_id)

    assert manager.flush(2)
    assert midterm_calls == [job_id]
    assert longterm_calls == [False, False]
    assert db.get_background_job(job_id)["status"] == "succeeded"
    assert manager.stop(timeout=1)


def test_stage_retry_does_not_change_successful_peer(db):
    job_id = _reserve(db, "independent-failure")
    midterm_job = db.claim_migration_stage(job_id, "midterm")
    assert midterm_job is not None
    longterm_job = db.claim_migration_stage(job_id, "longterm")
    assert longterm_job is not None
    assert db.mark_migration_stage_succeeded(job_id, "longterm", longterm_job["longterm_lease_token"])

    action = db.record_migration_stage_failure(
        job_id,
        "midterm",
        midterm_job["midterm_lease_token"],
        "temporary midterm failure",
        max_retries=1,
        retry_delay_seconds=60,
    )

    assert action == "retry"
    job = db.get_background_job(job_id)
    assert job["midterm_status"] == "retry"
    assert job["midterm_attempts"] == 1
    assert job["longterm_status"] == "succeeded"
    assert db.get_migration_job_messages(job_id)

    assert not hasattr(db, "retry_migration_stage")
    assert not hasattr(db, "retry_background_job")


def test_profile_completion_is_not_part_of_message_deletion_barrier(db):
    job_id = _reserve(db, "profile-independent")
    profile_job_id = db.create_profile_update_job("u1", _messages("profile-independent"))
    midterm_job = db.claim_migration_stage(job_id, "midterm")
    longterm_job = db.claim_migration_stage(job_id, "longterm")
    assert midterm_job
    assert longterm_job
    assert db.mark_migration_stage_succeeded(job_id, "midterm", midterm_job["midterm_lease_token"])
    assert db.get_migration_job_messages(job_id)
    assert db.mark_migration_stage_succeeded(job_id, "longterm", longterm_job["longterm_lease_token"])

    assert db.get_migration_job_messages(job_id) == []
    assert db.get_background_job(profile_job_id, "profile")["status"] == "pending"


def test_unfinished_sources_ignore_context_bridge_limits_and_failure_flags(db):
    job_id = _reserve(db, "context-bridge")
    claimed = db.claim_migration_stage(job_id, "midterm")
    assert claimed is not None

    processing = db.get_context_messages(
        "user_id=u1&run_id=r1",
        active_limit=0,
        include_pending=False,
        max_pending=0,
        include_failed=False,
    )
    assert [message["content"] for message in processing] == ["user-context-bridge", "assistant-context-bridge"]
    assert {message["status"] for message in processing} == {"processing"}

    db.mark_migration_stage_discarded(
        job_id,
        "midterm",
        claimed["midterm_lease_token"],
        "permanent failure",
    )
    failed = db.get_context_messages(
        "user_id=u1&run_id=r1",
        active_limit=0,
        include_pending=False,
        max_pending=0,
        include_failed=False,
    )
    assert [message["content"] for message in failed] == ["user-context-bridge", "assistant-context-bridge"]
    assert {message["status"] for message in failed} == {"pending"}


def _assert_exhausted_stage_is_discarded(db, stage):
    job_id = _reserve(db, f"{stage}-discarded")

    def failing_handler(job, messages, degraded):
        raise RuntimeError(f"{stage} {'degradation' if degraded else 'normal'} failed")

    handlers = {stage: failing_handler}
    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=0, retry_delays_seconds=(), poll_interval_seconds=0.01),
        **handlers,
    )
    try:
        manager.start()
        manager.wake_migration()
        assert manager.flush(2)
        job = db.get_background_job(job_id)
        assert job[f"{stage}_status"] == "discarded"
        assert job[f"{stage}_attempts"] == 1
        assert "degradation" in job[f"{stage}_last_error"]
        assert job[f"{stage}_finished_at"] is not None
        assert job["status"] == "completed_with_loss"
        assert job["finalized_at"] is not None
        assert db.get_migration_job_messages(job_id) == []
    finally:
        assert manager.stop(timeout=1)


def test_midterm_exhausted_and_degraded_failure_marks_discarded(db):
    _assert_exhausted_stage_is_discarded(db, "midterm")


def test_longterm_exhausted_and_degraded_failure_marks_discarded(db):
    _assert_exhausted_stage_is_discarded(db, "longterm")


def test_background_timeout_enters_existing_degraded_flow(db):
    job_id = _reserve(db, "provider-timeout")
    calls = []

    def longterm(job, messages, degraded):
        calls.append(degraded)
        if not degraded:
            raise TimeoutError("LLM request timed out after 60 seconds")

    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=0, poll_interval_seconds=0.01),
        longterm=longterm,
    )
    claimed = db.claim_migration_stage(job_id, "longterm")

    assert manager._run_migration_stage(claimed, "longterm", longterm)
    assert calls == [False, True]
    assert db.get_background_job(job_id)["longterm_status"] == "succeeded_degraded"


def test_missing_source_messages_marks_stage_discarded(db):
    job_id = _reserve(db, "missing-source")
    db.connection.execute("DELETE FROM messages WHERE migration_job_id = ?", (job_id,))
    db.connection.commit()
    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=3, retry_delays_seconds=(), poll_interval_seconds=0.01),
    )
    try:
        manager.start()
        manager.wake_migration()
        assert manager.flush(2)
        job = db.get_background_job(job_id)
        assert job["midterm_status"] == "discarded"
        assert job["longterm_status"] == "discarded"
        assert job["midterm_attempts"] == 0
        assert job["longterm_attempts"] == 0
        assert job["status"] == "completed_with_loss"
    finally:
        assert manager.stop(timeout=1)


def test_profile_exhaustion_marks_discarded(db):
    job_id = db.create_profile_update_job("profile-discard", _messages("profile-discard"))
    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=0, retry_delays_seconds=(), poll_interval_seconds=0.01),
        profile=lambda job: (_ for _ in ()).throw(RuntimeError("profile failed")),
    )
    try:
        manager.start()
        manager.wake_profile()
        assert manager.flush(2)
        job = db.get_background_job(job_id, "profile")
        assert job["status"] == "discarded"
        assert job["attempts"] == 1
        assert job["last_error"] == "profile failed"
    finally:
        assert manager.stop(timeout=1)


def _finalize_with_stage_statuses(db, midterm_status, longterm_status):
    job_id = _reserve(db, f"{midterm_status}-{longterm_status}")
    db.connection.execute(
        """
        UPDATE memory_migration_jobs
        SET midterm_status = ?, longterm_status = ?
        WHERE job_id = ?
        """,
        (midterm_status, longterm_status, job_id),
    )
    db.connection.commit()
    assert db.finalize_migration_job_if_ready(job_id) is True
    return job_id, db.get_background_job(job_id)


def test_one_discarded_one_succeeded_finalizes(db):
    job_id, job = _finalize_with_stage_statuses(db, "discarded", "succeeded")
    assert job["status"] == "completed_with_loss"
    assert job["finalized_at"] is not None
    assert db.get_migration_job_messages(job_id) == []


def test_one_discarded_one_degraded_finalizes(db):
    job_id, job = _finalize_with_stage_statuses(db, "discarded", "succeeded_degraded")
    assert job["status"] == "completed_with_loss"
    assert job["finalized_at"] is not None
    assert db.get_migration_job_messages(job_id) == []


def test_both_discarded_finalize(db):
    job_id, job = _finalize_with_stage_statuses(db, "discarded", "discarded")
    assert job["status"] == "completed_with_loss"
    assert job["finalized_at"] is not None
    assert db.get_migration_job_messages(job_id) == []


def test_discarded_but_other_stage_running_remains_in_context(db):
    job_id = _reserve(db, "discarded-running")
    midterm_job = db.claim_migration_stage(job_id, "midterm")
    assert midterm_job
    assert db.claim_migration_stage(job_id, "longterm")
    assert db.mark_migration_stage_discarded(
        job_id,
        "midterm",
        midterm_job["midterm_lease_token"],
        "midterm unavailable",
    )

    context = db.get_context_messages(
        "user_id=u1&run_id=r1",
        active_limit=0,
        include_pending=False,
        max_pending=0,
    )
    assert [message["content"] for message in context] == [
        "user-discarded-running",
        "assistant-discarded-running",
    ]
    assert db.get_background_job(job_id)["finalized_at"] is None


@pytest.mark.parametrize("stage", ["midterm", "longterm"])
def test_retry_blocks_next_same_session_stage(db, stage):
    first_job = _reserve(db, f"{stage}-retry-first")
    second_job = _reserve(db, f"{stage}-retry-second")
    claimed = db.claim_migration_stage(first_job, stage)
    assert claimed
    assert (
        db.record_migration_stage_failure(
            first_job,
            stage,
            claimed[f"{stage}_lease_token"],
            "temporary failure",
            max_retries=1,
            retry_delay_seconds=60,
        )
        == "retry"
    )
    assert db.claim_migration_stage(second_job, stage) is None


@pytest.mark.parametrize("stage", ["midterm", "longterm"])
def test_discarded_unblocks_next_same_session_stage(db, stage):
    first_job = _reserve(db, f"{stage}-discard-first")
    second_job = _reserve(db, f"{stage}-discard-second")
    claimed = db.claim_migration_stage(first_job, stage)
    assert claimed
    assert db.mark_migration_stage_discarded(
        first_job,
        stage,
        claimed[f"{stage}_lease_token"],
        "permanent failure",
    )
    assert db.claim_migration_stage(second_job, stage) is not None


def test_discarded_profile_unblocks_next_user_job(db):
    first_job = db.create_profile_update_job("ordered-profile", _messages("first"))
    second_job = db.create_profile_update_job("ordered-profile", _messages("second"))
    claimed = db.claim_profile_job(first_job)
    assert claimed
    assert (
        db.record_profile_failure(
            first_job,
            claimed["lease_token"],
            "permanent failure",
            max_retries=0,
            retry_delay_seconds=0,
        )
        == "discarded"
    )
    assert db.claim_profile_job(second_job) is not None


def test_discarded_jobs_cannot_return_to_pending(db):
    migration_job_id = _reserve(db, "cannot-retry")
    claimed_migration = db.claim_migration_stage(migration_job_id, "midterm")
    assert claimed_migration
    assert db.mark_migration_stage_discarded(
        migration_job_id,
        "midterm",
        claimed_migration["midterm_lease_token"],
        "permanent",
    )
    assert db.claim_migration_stage(migration_job_id, "midterm") is None

    profile_job_id = db.create_profile_update_job("cannot-retry", _messages("cannot-retry"))
    claimed_profile = db.claim_profile_job(profile_job_id)
    assert claimed_profile
    assert (
        db.record_profile_failure(
            profile_job_id,
            claimed_profile["lease_token"],
            "permanent",
            max_retries=0,
            retry_delay_seconds=0,
        )
        == "discarded"
    )
    assert db.claim_profile_job(profile_job_id) is None


def test_public_memory_api_has_no_manual_retry_dependency():
    assert not hasattr(SQLiteManager, "retry_migration_stage")
    assert not hasattr(SQLiteManager, "retry_background_job")
    assert not hasattr(Memory, "retry_background_job")
    assert not hasattr(AsyncMemory, "retry_background_job")


def test_retry_delay_configuration():
    assert _manager(MagicMock(), config=BackgroundTaskConfig(retry_delays_seconds=()))._retry_delay(5) == 0
    manager = _manager(MagicMock(), config=BackgroundTaskConfig(retry_delays_seconds=(0.25, 0.5)))
    assert manager._retry_delay(1) == 0.25
    assert manager._retry_delay(2) == 0.5
    assert manager._retry_delay(99) == 0.5
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        BackgroundTaskConfig(retry_delays_seconds=(0.0, -1.0))
    with pytest.raises(ValueError, match="less than lease_timeout_seconds"):
        BackgroundTaskConfig(lease_timeout_seconds=1, heartbeat_interval_seconds=1)


def test_unexpected_exhaustion_still_attempts_degradation(db):
    job_id = _reserve(db, "unexpected-degradation")
    job = db.claim_migration_stage(job_id, "midterm")
    calls = []

    def midterm(claimed_job, messages, degraded):
        calls.append(degraded)

    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=0, retry_delays_seconds=(), poll_interval_seconds=0.01),
        midterm=midterm,
    )
    manager._persist_unexpected_stage_failure(job, "midterm", midterm, RuntimeError("unexpected"))
    persisted = db.get_background_job(job_id)
    assert calls == [True]
    assert persisted["midterm_status"] == "succeeded_degraded"
    assert persisted["midterm_attempts"] == 1


def test_recovered_exhausted_stage_resumes_with_degradation_only(db):
    job_id = _reserve(db, "resume-degradation")
    claimed = db.claim_migration_stage(job_id, "longterm")
    assert claimed
    assert (
        db.record_migration_stage_failure(
            job_id,
            "longterm",
            claimed["longterm_lease_token"],
            "normal retries exhausted",
            max_retries=0,
            retry_delay_seconds=0,
        )
        == "exhausted"
    )
    db.connection.execute(
        """
        UPDATE memory_migration_jobs
        SET longterm_status = 'retry', longterm_next_retry_at = NULL
        WHERE job_id = ?
        """,
        (job_id,),
    )
    db.connection.commit()
    resumed_job = db.claim_migration_stage(job_id, "longterm")
    calls = []

    def longterm(claimed_job, messages, degraded):
        calls.append(degraded)

    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=0, retry_delays_seconds=(), poll_interval_seconds=0.01),
        longterm=longterm,
    )
    assert manager._run_migration_stage(resumed_job, "longterm", longterm)
    assert calls == [True]
    persisted = db.get_background_job(job_id)
    assert persisted["longterm_status"] == "succeeded_degraded"
    assert persisted["longterm_attempts"] == 1


def test_exhausted_longterm_uses_degraded_storage(db):
    job_id = _reserve(db, 1)
    calls = []

    def longterm(job, messages, degraded):
        calls.append(degraded)
        if not degraded:
            raise RuntimeError("LLM unavailable")

    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=0, poll_interval_seconds=0.01),
        longterm=longterm,
    )
    manager.start()
    manager.wake_migration()

    assert manager.flush(2)
    assert calls == [False, True]
    job = db.get_background_job(job_id)
    assert job["status"] == "succeeded_degraded"
    assert job["longterm_degraded"] is True
    assert db.get_migration_job_messages(job_id) == []
    assert manager.stop(timeout=1)


def test_failed_degradation_marks_discarded_and_does_not_block_next_job(db):
    first_job = _reserve(db, 1)
    second_job = _reserve(db, 2)

    def longterm(job, messages, degraded):
        if job["job_id"] == first_job:
            raise RuntimeError("all storage paths unavailable")

    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=0, poll_interval_seconds=0.01),
        longterm=longterm,
    )
    manager.start()
    manager.wake_migration()

    assert manager.flush(2)
    assert db.get_background_job(first_job)["status"] == "completed_with_loss"
    assert db.get_background_job(first_job)["longterm_status"] == "discarded"
    assert db.get_migration_job_messages(first_job) == []
    assert db.get_background_job(second_job)["status"] == "succeeded"
    assert manager.stop(timeout=1)


def test_same_session_migration_jobs_are_processed_in_sequence(db):
    job_ids = [_reserve(db, index) for index in range(3)]
    midterm_seen = []
    longterm_seen = []

    def midterm(job, messages, degraded):
        midterm_seen.append(job["job_id"])

    def longterm(job, messages, degraded):
        longterm_seen.append(job["job_id"])

    manager = _manager(db, midterm=midterm, longterm=longterm)
    manager.start()
    manager.wake_migration()

    assert manager.flush(2)
    assert midterm_seen == job_ids
    assert longterm_seen == job_ids
    assert manager.stop(timeout=1)


def test_same_user_profile_jobs_are_processed_in_sequence(db):
    job_ids = [db.create_profile_update_job("u1", _messages(index)) for index in range(3)]
    seen = []

    def profile(job):
        seen.append(job["job_id"])

    manager = _manager(db, profile=profile)
    manager.start()
    manager.wake_profile()

    assert manager.flush(2)
    assert seen == job_ids
    assert all(db.get_background_job(job_id, "profile")["status"] == "succeeded" for job_id in job_ids)
    assert manager.stop(timeout=1)


def test_retry_callback_can_make_vector_write_idempotent(db):
    job_id = _reserve(db, 1)
    persisted_job_ids = set()
    physical_writes = []
    attempts = 0

    def longterm(job, messages, degraded):
        nonlocal attempts
        attempts += 1
        if job["job_id"] not in persisted_job_ids:
            persisted_job_ids.add(job["job_id"])
            physical_writes.append(job["job_id"])
        if attempts == 1:
            raise RuntimeError("crash after vector write")

    manager = _manager(
        db,
        config=BackgroundTaskConfig(
            max_retries=1,
            retry_delays_seconds=(0.01,),
            poll_interval_seconds=0.005,
        ),
        longterm=longterm,
    )
    manager.start()
    manager.wake_migration()

    assert manager.flush(2)
    assert attempts == 2
    assert physical_writes == [job_id]
    assert manager.stop(timeout=1)


def test_longterm_degraded_storage_uses_stable_id_and_source_metadata(db):
    memory = _partial_memory(db, profile_enabled=False)
    memory.vector_store = MagicMock()
    memory.vector_store.get.side_effect = [None, SimpleNamespace(id="existing")]
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2]
    memory._create_memory = MagicMock(return_value="created")
    job = {
        "job_id": "migration-job-1",
        "metadata": {"user_id": "u1"},
    }

    memory._store_longterm_fallback(job, _messages(1))
    memory._store_longterm_fallback(job, _messages(1))

    expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "mem0:longterm-fallback:migration-job-1"))
    memory._create_memory.assert_called_once()
    args = memory._create_memory.call_args.args
    assert args[2] == {
        "user_id": "u1",
        "source_job_id": "migration-job-1",
        "source_stage": "longterm",
        "output_state": "staging",
        "output_lease_token": None,
        "memory_type": "raw_fallback",
        "needs_reprocessing": True,
        "degraded": True,
    }
    assert memory._create_memory.call_args.kwargs["memory_id"] == expected_id


def test_startup_recovers_pending_retry_and_stale_running_jobs(tmp_path):
    path = str(tmp_path / "recovery.db")
    original = SQLiteManager(path)
    pending_job = _reserve(original, "pending", scope="user_id=u1&run_id=pending")
    retry_job = _reserve(original, "retry", scope="user_id=u1&run_id=retry")
    running_job = _reserve(original, "running", scope="user_id=u1&run_id=running")
    assert original.claim_migration_stage(running_job, "midterm", lease_timeout_seconds=1)
    assert original.claim_migration_stage(running_job, "longterm", lease_timeout_seconds=1)

    stale_time = (beijing_now() - timedelta(seconds=10)).isoformat()
    original.connection.execute(
        """
        UPDATE memory_migration_jobs
        SET midterm_status = 'retry', midterm_next_retry_at = NULL,
            longterm_status = 'retry', longterm_next_retry_at = NULL
        WHERE job_id = ?
        """,
        (retry_job,),
    )
    original.connection.execute(
        """
        UPDATE memory_migration_jobs
        SET midterm_lease_expires_at = ?, longterm_lease_expires_at = ?, updated_at = ?
        WHERE job_id = ?
        """,
        (stale_time, stale_time, stale_time, running_job),
    )
    original.connection.execute(
        "UPDATE messages SET status = 'processing' WHERE migration_job_id = ?",
        (running_job,),
    )
    original.connection.commit()
    original.close()

    reopened = SQLiteManager(path)
    seen = []
    manager = _manager(
        reopened,
        config=BackgroundTaskConfig(
            poll_interval_seconds=0.01,
            lease_timeout_seconds=0.2,
            heartbeat_interval_seconds=0.05,
            watchdog_interval_seconds=0.05,
        ),
        midterm=lambda job, messages, degraded: seen.append(job["job_id"]),
    )
    try:
        manager.start()
        manager.wake_migration()
        assert manager.flush(2)
        assert set(seen) == {pending_job, retry_job, running_job}
        assert all(
            reopened.get_background_job(job_id)["status"] == "succeeded"
            for job_id in (pending_job, retry_job, running_job)
        )
    finally:
        manager.stop(timeout=1)
        reopened.close()


def test_flush_returns_false_on_timeout_then_true_after_release(db):
    _reserve(db, 1)
    started = threading.Event()
    release = threading.Event()

    def midterm(job, messages, degraded):
        started.set()
        release.wait(2)

    manager = _manager(db, midterm=midterm)
    manager.start()
    manager.wake_migration()
    assert started.wait(1)
    assert manager.flush(0.02) is False
    release.set()
    assert manager.flush(2) is True
    assert manager.stop(timeout=1)


def test_manager_stop_uses_shared_deadline_and_can_retry(db):
    _reserve(db, "blocked-stop")
    entered = threading.Event()
    release = threading.Event()

    def blocked_midterm(job, messages, degraded):
        entered.set()
        release.wait(2)

    manager = _manager(db, midterm=blocked_midterm)
    manager.start()
    manager.wake_midterm()
    assert entered.wait(1)

    started = time.monotonic()
    assert manager.stop(timeout=0.03) is False
    assert time.monotonic() - started < 0.3
    assert manager._started is True

    release.set()
    assert manager.stop(timeout=1) is True
    assert manager.threads_alive() is False


def _partial_memory(db, *, capacity=0, profile_enabled=True):
    memory = Memory.__new__(Memory)
    profile_config = UserProfileConfig(enabled=profile_enabled)
    memory.config = SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=True, short_term_capacity=capacity),
        profile=profile_config,
        background=BackgroundTaskConfig(
            max_retries=0,
            poll_interval_seconds=0.01,
            shutdown_timeout_seconds=2,
        ),
        history_db_path=db.db_path,
    )
    memory.db = db
    memory.api_version = "v1.1"
    memory.custom_instructions = None
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._profile_manager = ProfileManager(db, profile_config)
    memory._profile_updater = None
    memory._entity_store = None
    memory._component_init_lock = threading.RLock()
    return memory


def _partial_async_memory(db, *, capacity=0, profile_enabled=True):
    memory = AsyncMemory.__new__(AsyncMemory)
    profile_config = UserProfileConfig(enabled=profile_enabled)
    memory.config = SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=True, short_term_capacity=capacity),
        profile=profile_config,
        background=BackgroundTaskConfig(
            max_retries=0,
            poll_interval_seconds=0.01,
            shutdown_timeout_seconds=2,
        ),
        history_db_path=db.db_path,
    )
    memory.db = db
    memory.api_version = "v1.1"
    memory.custom_instructions = None
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._profile_manager = ProfileManager(db, profile_config)
    memory._profile_updater = None
    memory._profile_user_locks = {}
    memory._profile_user_locks_guard = threading.Lock()
    memory._entity_store = None
    memory._component_init_lock = threading.RLock()
    return memory


def _lazy_component_memory(memory_cls):
    memory = memory_cls.__new__(memory_cls)
    memory.config = SimpleNamespace(
        vector_store=SimpleNamespace(
            provider="test",
            config=SimpleNamespace(collection_name="memories"),
        ),
        midterm=SimpleNamespace(),
        profile=SimpleNamespace(),
    )
    memory.collection_name = "memories"
    memory.vector_store = SimpleNamespace()
    memory.embedding_model = SimpleNamespace()
    memory.llm = SimpleNamespace()
    memory.db = SimpleNamespace()
    memory._entity_store = None
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._profile_manager = None
    memory._profile_updater = None
    memory._component_init_lock = threading.RLock()
    return memory


def _concurrent_property_values(memory, property_name):
    barrier = threading.Barrier(5)
    values = []
    errors = []

    def read_property():
        try:
            barrier.wait(timeout=1)
            values.append(getattr(memory, property_name))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=read_property) for _ in range(4)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=1)
    for thread in threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(values) == 4
    assert all(value is values[0] for value in values)
    return values[0]


@pytest.mark.parametrize("memory_cls", [Memory, AsyncMemory])
def test_entity_store_initialized_once_under_concurrency(monkeypatch, memory_cls):
    memory = _lazy_component_memory(memory_cls)
    calls = []
    sentinel = object()

    def create(*args, **kwargs):
        calls.append((args, kwargs))
        time.sleep(0.02)
        return sentinel

    monkeypatch.setattr(memory_main.VectorStoreFactory, "create", create)
    assert _concurrent_property_values(memory, "entity_store") is sentinel
    assert len(calls) == 1


@pytest.mark.parametrize("memory_cls", [Memory, AsyncMemory])
def test_midterm_memory_initialized_once_under_concurrency(monkeypatch, memory_cls):
    memory = _lazy_component_memory(memory_cls)
    calls = []
    sentinel = object()

    def create(*args, **kwargs):
        calls.append((args, kwargs))
        time.sleep(0.02)
        return sentinel

    monkeypatch.setattr(memory_main, "MidTermMemory", create)
    assert _concurrent_property_values(memory, "midterm_memory") is sentinel
    assert len(calls) == 1


@pytest.mark.parametrize("memory_cls", [Memory, AsyncMemory])
def test_midterm_updater_initialized_once_under_concurrency(monkeypatch, memory_cls):
    memory = _lazy_component_memory(memory_cls)
    memory._midterm_memory = object()
    calls = []
    sentinel = object()

    def create(*args, **kwargs):
        calls.append((args, kwargs))
        time.sleep(0.02)
        return sentinel

    monkeypatch.setattr(memory_main, "MidTermUpdater", create)
    assert _concurrent_property_values(memory, "midterm_updater") is sentinel
    assert len(calls) == 1


@pytest.mark.parametrize("memory_cls", [Memory, AsyncMemory])
def test_midterm_retriever_initialized_once_under_concurrency(monkeypatch, memory_cls):
    memory = _lazy_component_memory(memory_cls)
    memory._midterm_memory = object()
    calls = []
    sentinel = object()

    def create(*args, **kwargs):
        calls.append((args, kwargs))
        time.sleep(0.02)
        return sentinel

    monkeypatch.setattr(memory_main, "MidTermRetriever", create)
    assert _concurrent_property_values(memory, "midterm_retriever") is sentinel
    assert len(calls) == 1


@pytest.mark.parametrize("memory_cls", [Memory, AsyncMemory])
def test_profile_components_initialized_once_under_concurrency(monkeypatch, memory_cls):
    memory = _lazy_component_memory(memory_cls)
    manager_calls = []
    updater_calls = []
    manager_sentinel = object()
    updater_sentinel = object()

    def create_manager(*args, **kwargs):
        manager_calls.append((args, kwargs))
        time.sleep(0.02)
        return manager_sentinel

    def create_updater(*args, **kwargs):
        updater_calls.append((args, kwargs))
        time.sleep(0.02)
        return updater_sentinel

    monkeypatch.setattr(memory_main, "ProfileManager", create_manager)
    monkeypatch.setattr(memory_main, "ProfileUpdater", create_updater)
    assert _concurrent_property_values(memory, "profile_manager") is manager_sentinel
    assert _concurrent_property_values(memory, "profile_updater") is updater_sentinel
    assert len(manager_calls) == 1
    assert len(updater_calls) == 1


def test_add_returns_without_waiting_for_migration_or_profile_llms(db, monkeypatch):
    memory = _partial_memory(db)
    migration_started = threading.Event()
    profile_started = threading.Event()
    release = threading.Event()

    def midterm(*args, **kwargs):
        migration_started.set()
        release.wait(2)
        return []

    class BlockingProfileUpdater:
        async def generate_update_plan_async(self, **kwargs):
            profile_started.set()
            release.wait(2)
            return ProfileUpdatePlan()

    memory._process_midterm_evictions = midterm
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    memory._profile_updater = BlockingProfileUpdater()
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice", lambda *args: None)

    started_at = time.monotonic()
    result = memory.add(_messages(1), user_id="u1", run_id="r1")
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert result["results"] == []
    assert result["background"]["migration_job_id"]
    assert result["background"]["profile_job_id"]
    assert db.get_migration_job_messages(result["background"]["migration_job_id"])
    assert migration_started.wait(1)
    assert profile_started.wait(1)

    release.set()
    assert memory.flush_background_tasks(2)
    memory.close()
    assert db.connection is None


def test_close_after_discarded_jobs(db, monkeypatch):
    memory = _partial_memory(db, profile_enabled=False)

    def fail_midterm(*args, **kwargs):
        raise RuntimeError("midterm unavailable")

    memory._process_midterm_evictions = fail_midterm
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice", lambda *args: None)

    result = memory.add(_messages("close-discarded"), user_id="u1", run_id="r1")
    assert memory.flush_background_tasks(2)
    job = db.get_background_job(result["background"]["migration_job_id"])
    assert job["status"] == "completed_with_loss"
    assert job["midterm_status"] == "discarded"

    memory.close()
    assert db.connection is None


@pytest.mark.asyncio
async def test_async_add_returns_without_waiting_for_migration_or_profile_llms(db, monkeypatch):
    memory = _partial_async_memory(db)
    migration_started = threading.Event()
    profile_started = threading.Event()
    release = threading.Event()

    def midterm(*args, **kwargs):
        migration_started.set()
        release.wait(2)
        return []

    class BlockingProfileUpdater:
        async def generate_update_plan_async(self, **kwargs):
            profile_started.set()
            release.wait(2)
            return ProfileUpdatePlan()

    memory._process_midterm_evictions = midterm
    memory._process_evicted_long_term_memories = AsyncMock(return_value=[])
    memory._profile_updater = BlockingProfileUpdater()
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice_async", AsyncMock())

    started_at = time.monotonic()
    result = await memory.add(_messages(1), user_id="u1", run_id="r1")
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert result["results"] == []
    assert result["background"]["migration_job_id"]
    assert result["background"]["profile_job_id"]
    assert db.get_migration_job_messages(result["background"]["migration_job_id"])
    assert migration_started.wait(1)
    assert profile_started.wait(1)

    release.set()
    assert await memory.flush_background_tasks(2)
    memory.close()
    assert db.connection is None


def test_close_waits_for_running_worker_before_closing_sqlite(db, monkeypatch):
    memory = _partial_memory(db, profile_enabled=False)
    started = threading.Event()
    release = threading.Event()

    def midterm(*args, **kwargs):
        started.set()
        release.wait(2)
        return []

    memory._process_midterm_evictions = midterm
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice", lambda *args: None)
    memory.add(_messages(1), user_id="u1", run_id="r1")
    assert started.wait(1)

    close_result = []
    close_thread = threading.Thread(target=lambda: close_result.append(memory.close()))
    close_thread.start()
    time.sleep(0.05)
    assert db.connection is not None
    release.set()
    close_thread.join(2)

    assert close_result == [True]
    assert db.connection is None


def test_reset_waits_for_worker_before_clearing_storage(db, monkeypatch):
    memory = _partial_memory(db, profile_enabled=False)
    started = threading.Event()
    release = threading.Event()

    def midterm(*args, **kwargs):
        started.set()
        release.wait(2)
        return []

    memory._process_midterm_evictions = midterm
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    memory._reset_midterm_state = MagicMock()
    memory.vector_store = MagicMock()
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice", lambda *args: None)
    memory.add(_messages(1), user_id="u1", run_id="r1")
    assert started.wait(1)

    reset_thread = threading.Thread(target=memory.reset)
    reset_thread.start()
    time.sleep(0.05)
    assert reset_thread.is_alive()
    assert db.connection is not None
    release.set()
    reset_thread.join(2)

    assert not reset_thread.is_alive()
    assert memory.db.connection is not None
    assert memory.db.background_jobs_pending() is False
    memory.close()
    assert memory.db is None

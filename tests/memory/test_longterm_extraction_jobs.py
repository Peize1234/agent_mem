from datetime import timedelta

from mem0.configs.base import BackgroundTaskConfig
from mem0.memory.background_worker import BackgroundWorkerManager
from mem0.memory.storage import SQLiteManager
from mem0.utils.timestamps import beijing_now


SCOPE = "run_id=r1&user_id=u1"
FILTERS = {"user_id": "u1", "run_id": "r1"}


def _qa(index: int):
    return [
        {"role": "user", "content": f"Q{index}"},
        {"role": "assistant", "content": f"A{index}"},
    ]


def _save(db, messages, *, capacity=100):
    return db.save_messages_and_create_background_jobs(
        messages,
        SCOPE,
        max_messages=capacity,
        filters=FILTERS,
        metadata={"source": "test"},
        infer=True,
        prompt=None,
        profile_user_id=None,
        create_longterm_jobs=True,
        return_longterm_job_ids=True,
    )


def test_complete_qa_immediately_creates_longterm_job_before_shortterm_overflow():
    db = SQLiteManager(":memory:")
    try:
        migration_id, _, job_ids = _save(db, _qa(1), capacity=100)

        assert migration_id is None
        assert len(job_ids) == 1
        job = db.get_background_job(job_ids[0], "longterm_extraction")
        assert job["turn_index"] == 1
        assert job["sequence_no"] == 1
        assert [message["role"] for message in job["messages"]] == ["user", "assistant"]
        assert job["filters"] == FILTERS
    finally:
        db.close()


def test_user_only_does_not_create_job_and_later_assistant_completes_same_turn():
    db = SQLiteManager(":memory:")
    try:
        assert _save(db, [{"role": "user", "content": "Q1"}])[2] == []
        job_ids = _save(db, [{"role": "assistant", "content": "A1"}])[2]

        assert len(job_ids) == 1
        job = db.get_background_job(job_ids[0], "longterm_extraction")
        assert job["turn_index"] == 1
        assert [message["content"] for message in job["messages"]] == ["Q1", "A1"]
    finally:
        db.close()


def test_same_session_longterm_jobs_claim_strictly_in_turn_order():
    db = SQLiteManager(":memory:")
    try:
        ids = [_save(db, _qa(index))[2][0] for index in range(1, 4)]

        first = db.claim_next_longterm_extraction_job()
        assert first["job_id"] == ids[0]
        assert db.claim_next_longterm_extraction_job() is None
        assert db.complete_longterm_extraction_job(first["job_id"], first["lease_token"])
        second = db.claim_next_longterm_extraction_job()
        assert second["job_id"] == ids[1]
        assert db.complete_longterm_extraction_job(second["job_id"], second["lease_token"])
        third = db.claim_next_longterm_extraction_job()
        assert third["job_id"] == ids[2]
    finally:
        db.close()


def test_retry_lease_heartbeat_and_watchdog_recovery():
    db = SQLiteManager(":memory:")
    try:
        job_id = _save(db, _qa(1))[2][0]
        claimed = db.claim_longterm_extraction_job(job_id, lease_timeout_seconds=60)
        assert db.longterm_extraction_job_lease_is_current(job_id, claimed["lease_token"])
        assert db.heartbeat_longterm_extraction_job(job_id, claimed["lease_token"], 60)
        assert (
            db.record_longterm_extraction_failure(
                job_id,
                claimed["lease_token"],
                "transient",
                max_retries=2,
                retry_delay_seconds=0,
            )
            == "retry"
        )
        retried = db.claim_longterm_extraction_job(job_id, lease_timeout_seconds=60)
        expired = (beijing_now() - timedelta(seconds=1)).isoformat()
        db.connection.execute(
            "UPDATE longterm_extraction_jobs SET lease_expires_at = ? WHERE job_id = ?",
            (expired, job_id),
        )
        db.connection.commit()

        recovered = db.recover_expired_background_leases(max_stale_recoveries=2)

        assert recovered["longterm_extraction"] == 1
        job = db.get_background_job(job_id, "longterm_extraction")
        assert job["status"] == "retry"
        assert job["recovery_count"] == 1
        assert not db.longterm_extraction_job_lease_is_current(job_id, retried["lease_token"])
    finally:
        db.close()


def test_idempotency_and_midterm_overflow_do_not_duplicate_longterm_extraction():
    db = SQLiteManager(":memory:")
    messages = [*_qa(1), *_qa(2)]
    kwargs = {
        "idempotency_key": "operation-1",
        "request_hash": "request-hash",
        "max_messages": 2,
        "filters": FILTERS,
        "metadata": {},
        "infer": True,
        "prompt": None,
        "profile_user_id": None,
    }
    try:
        first = db.save_background_add_idempotently(messages, SCOPE, **kwargs)
        second = db.save_background_add_idempotently(messages, SCOPE, **kwargs)

        assert second == first
        jobs = db.list_longterm_extraction_jobs(session_scope=SCOPE)
        assert [job["turn_index"] for job in jobs] == [1, 2]
        migration = db.get_background_job(first["background"]["migration_job_id"])
        assert migration["midterm_status"] == "pending"
        assert migration["longterm_status"] == "succeeded"
    finally:
        db.close()


def test_longterm_extraction_schema_keeps_required_durable_fields():
    db = SQLiteManager(":memory:")
    try:
        columns = {
            row[1] for row in db.connection.execute("PRAGMA table_info(longterm_extraction_jobs)").fetchall()
        }
        assert {
            "job_id",
            "session_scope",
            "turn_index",
            "messages_json",
            "filters_json",
            "metadata_json",
            "infer",
            "prompt",
            "status",
            "attempts",
            "next_retry_at",
            "last_error",
            "lease_token",
            "heartbeat_at",
            "lease_expires_at",
            "recovery_count",
            "sequence_no",
            "source_operation_key",
            "created_at",
            "updated_at",
        } <= columns
    finally:
        db.close()


def test_background_worker_retries_and_commits_fine_grained_job():
    db = SQLiteManager(":memory:")
    job_id = _save(db, _qa(1))[2][0]
    attempts = []
    commits = []
    discards = []

    def process(job):
        attempts.append(job["job_id"])
        if len(attempts) == 1:
            raise RuntimeError("transient extraction error")

    worker = BackgroundWorkerManager(
        db,
        BackgroundTaskConfig(
            max_retries=1,
            retry_delays_seconds=(0.0,),
            poll_interval_seconds=0.005,
            heartbeat_interval_seconds=0.01,
            lease_timeout_seconds=1.0,
        ),
        process_midterm=lambda *args: None,
        process_longterm=lambda *args: None,
        process_profile=lambda *args: None,
        process_longterm_extraction=process,
        commit_longterm_extraction_outputs=lambda job, token: commits.append((job["job_id"], token)),
        discard_longterm_extraction_outputs=lambda job, token: discards.append((job["job_id"], token)),
    )
    try:
        worker.start()
        worker.wake_longterm()

        assert worker.flush(2)
        assert attempts == [job_id, job_id]
        assert len(commits) == 1
        assert len(discards) == 1
        assert db.get_background_job(job_id, "longterm_extraction")["status"] == "succeeded"
    finally:
        worker.stop(timeout=1)
        db.close()

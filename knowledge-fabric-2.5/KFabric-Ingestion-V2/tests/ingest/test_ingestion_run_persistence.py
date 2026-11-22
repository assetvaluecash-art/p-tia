"""Focused coverage for durable job lifecycle, stage, and event persistence."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from kbfabric_ingest.ingest.repository.ingestion_run_repository import IngestionRunRepository


class _Cursor:
    def __init__(self, fetchone_values=None, fetchall_values=None, column_names=None):
        self.calls = []
        self.fetchone_values = list(fetchone_values or [("job-1",)])
        self.fetchall_values = list(fetchall_values or [])
        self.description = [Mock(name=name) for name in (column_names or [])]
        for column, name in zip(self.description, column_names or []):
            column.name = name
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def executemany(self, query, params):
        self.calls.append((query, list(params)))

    def fetchone(self):
        return self.fetchone_values.pop(0)

    def fetchall(self):
        return self.fetchall_values


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @contextmanager
    def cursor(self):
        yield self._cursor


def _repository_with_cursor():
    repository = IngestionRunRepository()
    cursor = _Cursor()
    repository._connect = Mock(return_value=_Connection(cursor))
    return repository, cursor


def test_run_creation_seeds_scope_and_all_stable_stage_rows():
    """Objective: verify run creation initializes audit, frozen-context stages, and event history atomically.

    This matters because the customer dashboard must be able to discover a run even if its worker
    starts later or fails during dispatch. The invariant is that the 15 stable stage rows and
    initial audit events are inserted in the same PostgreSQL transaction as the run record.
    """
    submitted_at = datetime.now(timezone.utc)
    cursor = _Cursor(fetchone_values=[(submitted_at, submitted_at)])
    repository = IngestionRunRepository()
    repository.logger = Mock()
    repository._connect = Mock(return_value=_Connection(cursor))
    record = {
        "run_id": "run-1", "job_id": "job-1", "ingestion_batch_id": "batch-1",
        "submitted_by": "user-1", "auth_source": "kong_jwt", "customer_id": "customer-1",
        "tenant_id": "tenant-1", "industry_id": "banking", "domain_id": "retail",
        "product_line_id": "pl-1", "customer_ontology_id": "ontology-1",
        "customer_ontology_version": 2, "canonical_ontology_id": "canonical-1",
        "canonical_ontology_version": "5.0.1", "extraction_policy_version": "5.0.1",
        "document_nature": "qe_assets", "hitl_mode": "review_ambiguous",
        "force_rewrite_requested": False, "run_context": {},
        "submission_metadata": {"project_ids": ["project-a"], "context_tags": ["system=core"]},
        "source_manifest": {"kind": "upload", "manifest_sha256": "abc123"},
    }

    timestamps = repository.create(record)

    assert timestamps["submitted_at"] == submitted_at.isoformat()
    run_insert = cursor.calls[0][1]
    assert run_insert["submission_metadata_json"] == '{"project_ids": ["project-a"], "context_tags": ["system=core"]}'
    assert run_insert["source_manifest_json"] == '{"kind": "upload", "manifest_sha256": "abc123"}'
    stage_seed = next(call for call in cursor.calls if call[0].startswith("INSERT INTO ingestion_run_stage"))
    assert len(stage_seed[1]) == 15
    assert stage_seed[1][0] == ("run-1", "run.queued")
    assert stage_seed[1][-1] == ("run-1", "run.verify_report")
    assert sum("INSERT INTO ingestion_run_event" in query for query, _ in cursor.calls) == 3
    repository._connect.assert_called_once()


def test_job_list_query_applies_owner_filters_and_bounded_pagination():
    """Objective: verify durable list SQL scopes rows before applying page limits."""
    row = ("job-1", "run-1", "batch-1", "user-1", None, None, None, None,
           "RUNNING", "customer-1", "banking", "cards")
    cursor = _Cursor(
        fetchone_values=[(1,)], fetchall_values=[row],
        column_names=["job_id", "run_id", "ingestion_batch_id", "submitted_by",
                      "submitted_at", "started_at", "completed_at", "updated_at",
                      "status", "customer_id", "domain_id", "product_line_id"],
    )
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    jobs, total = repository.list_jobs(
        submitted_by="user-1", limit=500, offset=3, status="RUNNING",
        customer_id="customer-1", domain_id="banking", product_line_id="cards",
    )

    assert total == 1
    assert jobs[0]["job_id"] == "job-1"
    list_query, list_params = cursor.calls[1]
    assert "submitted_by = %s" in list_query
    assert "status = %s" in list_query
    assert "customer_id = %s" in list_query
    assert list_params == ["user-1", "RUNNING", "customer-1", "banking", "cards", 200, 3]


def test_stage_transition_and_event_are_written_in_one_transaction():
    """Objective: verify stage state and its ordered event use the same database transaction.

    This matters because the dashboard and future SSE replay must never show an event whose
    corresponding stage state was not committed. The invariant is that both writes share one
    connection and include the same run, stage, and status.
    """
    repository, cursor = _repository_with_cursor()

    repository.record_stage(
        "run-1", "lane2.pass2", "RUNNING", progress={"blocks_done": 3, "blocks_total": 9}
    )

    assert len(cursor.calls) == 3
    assert "UPDATE ingestion_run_stage" in cursor.calls[0][0]
    assert cursor.calls[0][1][0] == "RUNNING"
    assert "SELECT job_id FROM ingestion_run" in cursor.calls[1][0]
    assert "INSERT INTO ingestion_run_event" in cursor.calls[2][0]
    assert cursor.calls[2][1][1:6] == (
        "run-1", "job-1", "stage.running", "lane2.pass2", "RUNNING"
    )
    repository._connect.assert_called_once()


def test_retry_transition_increments_attempt_and_restarts_stage_clock():
    """A retried stage gets a fresh start time and a monotonic attempt number."""
    repository, cursor = _repository_with_cursor()

    repository.record_stage("run-1", "lane2.pass2", "RUNNING")

    update_sql, params = cursor.calls[0]
    assert "status IN ('FAILED', 'RETRY_WAIT', 'SUCCEEDED', 'SKIPPED')" in update_sql
    assert "attempt + 1" in update_sql
    assert "status <> 'RUNNING' THEN CURRENT_TIMESTAMP" in update_sql
    assert params[:5] == ("RUNNING", None, "RUNNING", "RUNNING", "RUNNING")


def test_stage_terminal_transition_preserves_prior_progress_when_no_new_counts_arrive():
    """A status-only completion must not erase the latest safe stage counters."""
    repository, cursor = _repository_with_cursor()

    repository.record_stage("run-1", "lane2.pass2", "SUCCEEDED")

    update_sql, params = cursor.calls[0]
    assert "progress_json = COALESCE(%s::jsonb, progress_json)" in update_sql
    assert params[6] is None


def test_completed_run_with_pending_reviews_keeps_review_pending_status():
    """A pipeline completion is labeled review-pending when HITL decisions remain."""
    cursor = _Cursor(fetchone_values=[(True,), ("job-1",)])
    repository = IngestionRunRepository()
    repository.logger = Mock()
    repository._connect = Mock(return_value=_Connection(cursor))

    repository.update_status("run-1", "COMPLETED")

    assert "FROM hitl_review" in cursor.calls[0][0]
    assert cursor.calls[1][1] == (
        "COMPLETED_WITH_REVIEW_PENDING", "COMPLETED_WITH_REVIEW_PENDING",
        "COMPLETED_WITH_REVIEW_PENDING", None, None, "run-1",
    )
    assert cursor.calls[-1][1][5] == "COMPLETED_WITH_REVIEW_PENDING"


def test_unknown_stage_is_rejected_before_database_access():
    """Objective: reject misspelled stage IDs before writing audit data.

    This matters because stage IDs are stable UI/API identifiers. The invariant is that an
    unknown ID cannot silently create a second, incompatible stage vocabulary in PostgreSQL.
    """
    repository, _ = _repository_with_cursor()

    with pytest.raises(ValueError, match="Unknown ingestion stage"):
        repository.record_stage("run-1", "lane2.pass-two", "RUNNING")

    repository._connect.assert_not_called()


def test_progress_event_contains_only_supplied_safe_snapshot():
    """Objective: verify a progress update is stored as an ordered job event.

    This matters for customer dashboards and replay after reconnect. The invariant is that the
    event is scoped to the run/job and includes only the phase and aggregate counters supplied by
    the worker, not document text or model payloads.
    """
    repository, cursor = _repository_with_cursor()

    repository.record_progress("run-1", "Information Extraction", {"entities_found": 12})

    assert len(cursor.calls) == 2
    assert cursor.calls[0][1] == ("run-1",)
    assert "INSERT INTO ingestion_run_event" in cursor.calls[1][0]
    assert cursor.calls[1][1][1:6] == (
        "run-1", "job-1", "job.progress", None, "RUNNING"
    )


def test_checkpoint_commit_persists_artifact_provenance_and_activity_event():
    """A checkpoint references an immutable artifact and binds it to inputs and context."""
    repository = IngestionRunRepository()
    cursor = _Cursor(fetchone_values=[(2,), ("job-1",)])
    repository._connect = Mock(return_value=_Connection(cursor))
    digest = "a" * 64

    checkpoint_id = repository.commit_checkpoint(
        "run-1", "lane2.pass1", attempt=2, unit_key="block-0004",
        artifact_uri="postgres://artifact-store/run-1/block-0004",
        artifact_sha256=digest, source_manifest_sha256="b" * 64,
        context_sha256="c" * 64, cursor={"next_block": 5},
    )

    assert len(checkpoint_id) == 36
    assert "INSERT INTO ingestion_run_checkpoint" in cursor.calls[1][0]
    assert cursor.calls[1][1][0:5] == (
        checkpoint_id, "run-1", "lane2.pass1", 2, "block-0004"
    )
    assert "FOR UPDATE" in cursor.calls[0][0]
    assert "SET checkpoint_id = %s" in cursor.calls[2][0]
    assert "INSERT INTO ingestion_run_event" in cursor.calls[4][0]
    assert cursor.calls[4][1][3] == "checkpoint.committed"


def test_worker_start_completes_the_queued_business_stage():
    """The persisted queue stage must not remain pending once a worker claims the run."""
    repository = IngestionRunRepository()
    cursor = _Cursor(fetchone_values=[("job-1", "QUEUED")])
    repository._connect = Mock(return_value=_Connection(cursor))

    assert repository.start_worker("run-1") is True

    assert any(
        "stage_id = 'run.queued'" in query and "SET status = 'SUCCEEDED'" in query
        for query, _params in cursor.calls
    )
    assert sum("INSERT INTO ingestion_run_event" in query for query, _params in cursor.calls) == 2


def test_store_write_unit_persists_status_and_safe_reconciliation_event():
    """Store ledger state and its event are committed together for resume reconciliation."""
    repository, cursor = _repository_with_cursor()

    repository.record_store_write_unit(
        "run-1", "neo4j", "blocks:1:abc123", "UNKNOWN", attempt=2,
        progress={"batch": 1, "block_count": 8},
        error={"code": "DEPENDENCY_FAILURE", "message": "connection lost"},
    )

    assert len(cursor.calls) == 3
    assert "INSERT INTO ingestion_run_store_write" in cursor.calls[0][0]
    assert cursor.calls[0][1][0:5] == (
        "run-1", "neo4j", "blocks:1:abc123", "UNKNOWN", 2,
    )
    assert "SELECT job_id FROM ingestion_run" in cursor.calls[1][0]
    assert "INSERT INTO ingestion_run_event" in cursor.calls[2][0]
    assert cursor.calls[2][1][3] == "store.write_status_changed"
    assert json.loads(cursor.calls[2][1][6])["error_code"] == "DEPENDENCY_FAILURE"


@pytest.mark.parametrize("store,status", [("bad-store", "SUCCEEDED"), ("weaviate", "BROKEN")])
def test_store_write_unit_rejects_unknown_store_or_status_before_database_access(store, status):
    """Invalid reconciliation labels cannot create a second store/status vocabulary."""
    repository, _ = _repository_with_cursor()
    with pytest.raises(ValueError):
        repository.record_store_write_unit("run-1", store, "unit", status, attempt=1)
    repository._connect.assert_not_called()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"stage_id": "made.up"}, "Unknown ingestion stage"),
        ({"attempt": 0}, "attempt, unit key, and artifact URI"),
        ({"artifact_sha256": "not-a-digest"}, "artifact_sha256"),
    ],
)
def test_invalid_checkpoint_is_rejected_before_database_access(overrides, message):
    repository, _ = _repository_with_cursor()
    values = {
        "stage_id": "lane2.pass1", "attempt": 1, "unit_key": "block-1",
        "artifact_uri": "s3://bucket/checkpoint", "artifact_sha256": "a" * 64,
        "source_manifest_sha256": "b" * 64, "context_sha256": "c" * 64,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        repository.commit_checkpoint("run-1", **values)

    repository._connect.assert_not_called()


def test_resume_claim_preserves_stages_through_checkpoint_and_resets_only_later_stages():
    """Resume keeps completed history/checkpoints and reopens only downstream stages."""
    cursor = _Cursor(fetchone_values=[("FAILED",), None,
        ("checkpoint-1", "lane2.pass2", "/tmp/cp.json", "a" * 64, "b" * 64, "c" * 64),
        ("PENDING",), (2,), ("job-1",)])
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    result = repository.claim_resume(
        "run-1", idempotency_key="resume-key-001", requested_by="user-1",
        reason="Gateway retries exhausted",
    )

    assert result["attempt"] == 2
    assert result["checkpoint_stage"] == "lane2.pass2"
    assert result["resume_from_stage"] == "ontology.admission"
    assert result["already_claimed"] is False
    assert "FOR UPDATE" in cursor.calls[0][0]
    assert "stores.write" in cursor.calls[3][0]
    assert "INSERT INTO ingestion_run_attempt" in cursor.calls[5][0]
    assert "SET status = 'QUEUED'" in cursor.calls[6][0]
    assert cursor.calls[6][1] == (0, "run-1")
    assert all("attempt + 1" in call[0] for call in cursor.calls[7:-2])
    assert "INSERT INTO ingestion_run_event" in cursor.calls[-1][0]


def test_resume_claim_after_store_success_enters_report_only_mode():
    """A verified stores.write report-input checkpoint skips both knowledge stores."""
    cursor = _Cursor(fetchone_values=[("FAILED",), None,
        ("checkpoint-1", "stores.write", "/tmp/cp.json", "a" * 64, "b" * 64, "c" * 64),
        ("SUCCEEDED",), (2,), ("job-1",)])
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    result = repository.claim_resume(
        "run-1", idempotency_key="resume-report-001", requested_by="user-1",
    )

    assert result["checkpoint_stage"] == "stores.write"
    assert result["resume_from_stage"] == "run.verify_report"
    assert result["reconciliation_mode"] == "REPORT_ONLY"
    stage_resets = [params for query, params in cursor.calls if "UPDATE ingestion_run_stage" in query]
    assert stage_resets == [("run-1", "run.verify_report")]


def test_automatic_resume_claim_increments_only_the_persisted_auto_retry_counter():
    cursor = _Cursor(fetchone_values=[("FAILED",), None,
        ("checkpoint-1", "lane2.pass2", "/tmp/cp.json", "a" * 64, "b" * 64, "c" * 64),
        ("PENDING",), (2,), ("job-1",)])
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    result = repository.claim_resume(
        "run-1", idempotency_key="auto-retry-run-2", requested_by="system:auto-retry",
        reason="Transient dependency retry", automatic_retry=True,
    )

    assert result["already_claimed"] is False
    assert cursor.calls[6][1] == (1, "run-1")


def test_auto_retry_candidate_query_requires_retryable_error_and_remaining_budget():
    cursor = _Cursor(fetchall_values=[], column_names=["job_id", "run_id", "submitted_by", "auth_source", "auto_retry_count"])
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    assert repository.list_auto_retry_candidates(max_attempts=2) == []
    query, params = cursor.calls[0]
    assert "error_json->>'retryable'" in query
    assert "auto_retry_count < %s" in query
    assert params == (2, 50)


def test_cancel_request_is_durable_and_only_allowed_before_publishing():
    """A cancel request is persisted while stores are pending, with actor audit evidence."""
    cursor = _Cursor(fetchone_values=[("run-1", "user-1", "RUNNING"), ("PENDING",)])
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    result = repository.request_cancel("job-1", requested_by="user-1")

    assert result["status"] == "CANCEL_REQUESTED"
    assert cursor.calls[0][1] == ("job-1",)
    assert "FOR UPDATE" in cursor.calls[0][0]
    assert "SET status = 'CANCEL_REQUESTED'" in cursor.calls[2][0]
    assert cursor.calls[-1][1][3] == "job.cancel_requested"


def test_publish_boundary_observes_cancel_winner_without_starting_store_stage():
    """If cancellation committed first, the worker cannot enter publishing."""
    cursor = _Cursor(fetchone_values=[("job-1", "CANCEL_REQUESTED")])
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    assert repository.begin_store_publishing("run-1") is False
    assert len(cursor.calls) == 1


def test_queued_cancel_wins_before_worker_start():
    """A worker cannot overwrite a cancellation that committed while it was queued."""
    cursor = _Cursor(fetchone_values=[("job-1", "CANCEL_REQUESTED")])
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    assert repository.start_worker("run-1") is False
    assert len(cursor.calls) == 1


def test_resume_claim_rejects_runs_that_started_store_publishing():
    cursor = _Cursor(fetchone_values=[("FAILED",), None,
        ("checkpoint-1", "lane2.pass2", "/tmp/cp.json", "a" * 64, "b" * 64, "c" * 64),
        ("FAILED",)])
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    with pytest.raises(ValueError, match="no admission checkpoint"):
        repository.claim_resume(
            "run-1", idempotency_key="resume-key-002", requested_by="user-1"
        )

    assert len(cursor.calls) == 4


def test_resume_claim_replays_interrupted_stores_from_admission_checkpoint():
    """A partial/abandoned publish replays only deterministic store upserts from its saved plan."""
    cursor = _Cursor(fetchone_values=[("FAILED",), None,
        ("checkpoint-1", "admission.plan", "/tmp/cp.json", "a" * 64, "b" * 64, "c" * 64),
        ("FAILED",), (3,), ("job-1",)])
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    result = repository.claim_resume(
        "run-1", idempotency_key="resume-key-004", requested_by="user-1"
    )

    assert result["checkpoint_stage"] == "admission.plan"
    assert result["resume_from_stage"] == "stores.write"
    assert result["reconciliation_mode"] == "IDEMPOTENT_STORE_REPLAY"


def test_resume_claim_replays_same_idempotency_key_without_new_attempt():
    cursor = _Cursor(fetchone_values=[("QUEUED",), (2, "resume-existing", "QUEUED")])
    repository = IngestionRunRepository()
    repository._connect = Mock(return_value=_Connection(cursor))

    result = repository.claim_resume(
        "run-1", idempotency_key="resume-key-003", requested_by="user-1"
    )

    assert result == {
        "attempt": 2, "ray_submission_id": "resume-existing",
        "status": "QUEUED", "already_claimed": True,
    }
    assert len(cursor.calls) == 2


def test_run_schema_defines_stage_snapshot_and_ordered_event_ledger():
    """Objective: protect additive PostgreSQL schema for dashboard history and event replay.

    This matters because the UI and customer-level metrics need durable per-run provenance across
    worker restarts. The invariant is that schema startup creates 15 stable stage rows per run and
    an event sequence indexed by run ID without touching graph/vector stores.
    """
    sql = Path("kbfabric_ingest/ingest/sql/ingestion_run.sql").read_text(encoding="utf-8")

    assert len(IngestionRunRepository.STAGES) == 15
    assert "CREATE TABLE IF NOT EXISTS ingestion_run_stage" in sql
    assert "CREATE TABLE IF NOT EXISTS ingestion_run_event" in sql
    assert "CREATE TABLE IF NOT EXISTS ingestion_run_checkpoint" in sql
    assert "CREATE TABLE IF NOT EXISTS ingestion_run_store_write" in sql
    assert "ADD COLUMN IF NOT EXISTS error_json JSONB" in sql
    assert "ADD COLUMN IF NOT EXISTS auto_retry_count INTEGER" in sql
    assert "artifact_sha256 ~ '^[0-9a-f]{64}$'" in sql
    assert "idx_ingestion_run_event_replay" in sql
    assert "COMPLETED_WITH_REVIEW_PENDING" in sql

"""Focused API coverage for durable job snapshots and replayable event access."""

from datetime import datetime, timezone
import hashlib
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from kbfabric_ingest.scheduler import api
from kbfabric_ingest.security.auth import AuthenticatedActor


def _details(owner="operator-1"):
    """Build the minimum durable snapshot needed by the read endpoints."""
    return {
        "job_id": "job-1",
        "run_id": "run-1",
        "ingestion_batch_id": "batch-1",
        "submitted_by": owner,
        "status": "RUNNING",
        "stages": [{"stage_id": f"stage-{n}", "status": "PENDING"} for n in range(15)],
    }


@pytest.mark.asyncio
async def test_job_details_are_limited_to_submitter_or_jobs_admin(monkeypatch):
    """Objective: verify a durable run snapshot cannot be read across user ownership.

    This keeps job metadata private while customer/project entitlement claims are not yet
    available from the auth service. The same 404 is used for absent and unauthorized jobs.
    """
    repository = Mock()
    repository.get_job_details.return_value = _details()
    monkeypatch.setattr(api, "IngestionRunRepository", lambda: repository)
    monkeypatch.setattr(api, "read_progress_file", lambda _job_id: {"progress_pct": 42})
    monkeypatch.setattr(api, "RAY_DASHBOARD_PUBLIC_URL", "https://ray.example")

    with pytest.raises(HTTPException) as denied:
        await api.get_job_status("job-1", AuthenticatedActor("other-user", "kong_jwt"))
    assert denied.value.status_code == 404

    result = await api.get_job_status("job-1", AuthenticatedActor("operator-1", "kong_jwt"))
    assert result["progress_pct"] == 42
    assert len(result["stages"]) == 15
    assert result["ray_dashboard_url"] == "https://ray.example/#/jobs/job-1"


@pytest.mark.asyncio
async def test_cancel_endpoint_persists_request_for_owner(monkeypatch):
    repository = Mock()
    repository.get_resume_record.return_value = {
        "run_id": "run-1", "submitted_by": "operator-1"
    }
    repository.request_cancel.return_value = {
        "run_id": "run-1", "status": "CANCEL_REQUESTED", "already_requested": False
    }
    monkeypatch.setattr(api, "IngestionRunRepository", lambda: repository)

    result = await api.cancel_ingestion_job(
        "job-1", AuthenticatedActor("operator-1", "kong_jwt")
    )

    assert result["cancel_requested"] is True
    repository.request_cancel.assert_called_once_with("job-1", requested_by="operator-1")


@pytest.mark.asyncio
async def test_cancel_endpoint_hides_jobs_from_other_actors(monkeypatch):
    repository = Mock()
    repository.get_resume_record.return_value = {
        "run_id": "run-1", "submitted_by": "owner"
    }
    monkeypatch.setattr(api, "IngestionRunRepository", lambda: repository)

    with pytest.raises(HTTPException) as denied:
        await api.cancel_ingestion_job(
            "job-1", AuthenticatedActor("stranger", "kong_jwt")
        )

    assert denied.value.status_code == 404
    repository.request_cancel.assert_not_called()


@pytest.mark.asyncio
async def test_auto_retry_supervisor_dispatches_through_checkpoint_resume(monkeypatch):
    repository = Mock()
    repository.list_auto_retry_candidates.return_value = [{
        "job_id": "job-1", "run_id": "run-1", "submitted_by": "operator-1",
        "auth_source": "kong_jwt", "auto_retry_count": 0,
    }]
    monkeypatch.setattr(api, "IngestionRunRepository", lambda: repository)
    dispatched = []

    async def fake_resume(*args):
        dispatched.append(args)
        return {"status": "QUEUED"}

    async def stop_after_one_poll(_seconds):
        raise asyncio.CancelledError

    import asyncio
    monkeypatch.setattr(api, "resume_ingestion_job", fake_resume)
    monkeypatch.setattr(api.asyncio, "sleep", stop_after_one_poll)
    monkeypatch.setattr(
        "kbfabric_ingest.ingest.retry_policy.automatic_retry_max_attempts", lambda: 2
    )
    monkeypatch.setattr(
        "kbfabric_ingest.ingest.retry_policy.automatic_retry_poll_seconds", lambda: 5
    )

    with pytest.raises(asyncio.CancelledError):
        await api._automatic_retry_supervisor()

    assert len(dispatched) == 1
    assert dispatched[0][0] == "job-1"
    assert dispatched[0][2] == "auto-retry-run-1-1"
    assert dispatched[0][3].auth_source == "internal"


@pytest.mark.asyncio
async def test_job_event_stream_replays_persisted_events_after_cursor(monkeypatch):
    """Objective: verify SSE emits persisted events with replay IDs and ordered event data.

    Reconnect behavior depends on event IDs being carried by SSE frames so the browser can
    send Last-Event-ID on its next connection and continue after the committed cursor.
    """
    event = {
        "event_sequence": 8,
        "event_id": "evt-8",
        "run_id": "run-1",
        "job_id": "job-1",
        "event_type": "stage.running",
        "stage_id": "lane2.pass2",
        "status": "RUNNING",
        "payload_json": {"blocks_done": 2},
        "occurred_at": datetime.now(timezone.utc),
    }
    repository = Mock()
    repository.get_job_details.return_value = _details()
    repository.resolve_event_cursor.return_value = 7
    repository.list_events_after.return_value = [event]
    monkeypatch.setattr(api, "IngestionRunRepository", lambda: repository)

    class DisconnectAfterFirstFrame:
        def __init__(self):
            self.calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1

    response = await api.stream_job_events(
        "job-1",
        DisconnectAfterFirstFrame(),
        "evt-7",
        AuthenticatedActor("operator-1", "kong_jwt"),
    )
    frame = await anext(response.body_iterator)

    assert response.media_type == "text/event-stream"
    assert "id: evt-8" in frame
    assert "event: stage.running" in frame
    assert '"event_sequence":8' in frame
    repository.resolve_event_cursor.assert_called_once_with("job-1", "evt-7")
    repository.list_events_after.assert_called_once_with("job-1", 7, 100)


@pytest.mark.asyncio
async def test_job_list_uses_postgres_scope_and_filters_for_actor(monkeypatch):
    """Objective: verify the UI job list reads durable jobs and defaults to caller scope."""
    repository = Mock()
    repository.list_jobs.return_value = ([{"job_id": "job-1", "status": "RUNNING"}], 1)
    monkeypatch.setattr(api, "IngestionRunRepository", lambda: repository)

    result = await api.list_jobs(
        limit=25, offset=5, status="RUNNING", customer_id="customer-1",
        domain_id="banking", product_line_id="cards",
        actor=AuthenticatedActor("operator-1", "kong_jwt"),
    )

    assert result.total == 1
    assert result.jobs[0]["job_id"] == "job-1"
    repository.list_jobs.assert_called_once_with(
        submitted_by="operator-1", read_all=False, limit=25, offset=5,
        status="RUNNING", customer_id="customer-1", domain_id="banking",
        product_line_id="cards",
    )


def test_directory_source_manifest_hashes_supported_files_and_ignores_other_files(tmp_path):
    """Objective: record a reproducible source inventory without persisting file content."""
    (tmp_path / "nested").mkdir()
    source = tmp_path / "nested" / "requirements.txt"
    source.write_bytes(b"source requirements")
    (tmp_path / "notes.md").write_text("not an ingestion input", encoding="utf-8")

    manifest = api._build_directory_source_manifest(tmp_path, "/worker/input")

    assert manifest["inventory_complete"] is True
    assert manifest["worker_input_dir"] == "/worker/input"
    assert manifest["files"] == [{
        "name": "nested/requirements.txt",
        "size_bytes": len(b"source requirements"),
        "sha256": hashlib.sha256(b"source requirements").hexdigest(),
    }]
    assert len(manifest["manifest_sha256"]) == 64


def test_directory_source_manifest_marks_missing_directory_incomplete(tmp_path):
    """Objective: avoid treating a missing input path as a verified empty source set."""
    manifest = api._build_directory_source_manifest(tmp_path / "missing", "/worker/missing")

    assert manifest["inventory_complete"] is False
    assert manifest["files"] == []


def test_resume_source_manifest_rejects_changed_or_deleted_sources(tmp_path):
    """Resume must stop if the original source inventory has changed since submission."""
    source = tmp_path / "requirements.txt"
    source.write_text("original", encoding="utf-8")
    manifest = api._build_directory_source_manifest(tmp_path, "/worker/input")

    assert api._verify_resume_source_manifest(manifest) == "/worker/input"
    source.write_text("changed", encoding="utf-8")
    assert api._verify_resume_source_manifest(manifest) is None


@pytest.mark.asyncio
async def test_resume_redispatches_same_run_with_idempotent_attempt(monkeypatch, tmp_path):
    """Resume keeps logical provenance, reuses frozen context, and uses a distinct Ray ID."""
    record = {
        "run_id": "run-1", "job_id": "job-1", "ingestion_batch_id": "batch-1",
        "submitted_by": "operator-1", "auth_source": "kong_jwt", "status": "FAILED",
        "customer_id": "customer-1", "tenant_id": "tenant-1", "industry_id": "banking",
        "domain_id": "banking", "product_line_id": "cards",
        "customer_ontology_id": "ontology-1", "customer_ontology_version": 2,
        "canonical_ontology_id": "canonical-1", "canonical_ontology_version": "1",
        "extraction_policy_version": "2", "document_nature": "qe_assets",
        "hitl_mode": "review_ambiguous", "force_rewrite_requested": False,
        "run_context_json": {
            "customer_id": "customer-1", "tenant_id": "tenant-1", "industry_id": "banking",
            "domain_id": "banking", "product_line_id": "cards",
            "customer_ontology_id": "ontology-1", "customer_ontology_version": 2,
            "canonical_ontology_id": "canonical-1", "canonical_ontology_version": "1",
            "extraction_policy_version": "2",
        },
        "source_manifest_json": {"inventory_complete": True, "files": [{"name": "a.txt"}]},
        "stores_write_status": "PENDING",
    }
    repository = Mock()
    repository.get_resume_record.return_value = record
    repository.get_resume_attempt.return_value = None
    from kbfabric_ingest.ingest.checkpoints import stable_digest, write_checkpoint_artifact
    monkeypatch.setattr(api, "BASE_DIR", tmp_path)
    checkpoint_dir = tmp_path / "checkpoints" / "run-1"
    checkpoint_file = checkpoint_dir / "lane2.pass2.json"
    artifact_digest = write_checkpoint_artifact(
        checkpoint_file, stage_id="lane2.pass2", payload={"block_kind": "enriched", "blocks": []}
    )
    repository.get_latest_checkpoint.return_value = {
        "stage_id": "lane2.pass2", "artifact_uri": str(checkpoint_file),
        "artifact_sha256": artifact_digest,
        "source_manifest_sha256": stable_digest(record["source_manifest_json"]),
        "context_sha256": stable_digest(record["run_context_json"]),
    }
    repository.claim_resume.return_value = {
        "attempt": 2, "ray_submission_id": "resume-uuid", "status": "QUEUED",
        "already_claimed": False, "resume_from_stage": "ontology.admission",
        "checkpoint_stage": "lane2.pass2", "checkpoint_id": "cp-1",
        "reconciliation_mode": "CONTINUE_FROM_CHECKPOINT",
    }
    client = Mock()
    client.list_jobs.return_value = []
    monkeypatch.setattr(api, "IngestionRunRepository", lambda: repository)
    monkeypatch.setattr(api, "_verify_resume_source_manifest", lambda _manifest: str(tmp_path))
    monkeypatch.setattr(api, "get_app_config", lambda: {"scheduler": {"max_concurrent_jobs": 10}})
    monkeypatch.setattr(api, "get_job_client", lambda: client)
    monkeypatch.setattr(api, "check_domain_lock", lambda *_args: (False, None))
    monkeypatch.setattr(api, "_resolve_ray_worker_dir", lambda _persist: "/ray")
    monkeypatch.setattr(api, "_get_ray_python_path", lambda _persist: "/venv/python")
    monkeypatch.setattr(api, "_get_ray_runtime_env", lambda *_a, **_kw: {"env_vars": {}})

    result = await api.resume_ingestion_job(
        "job-1", api.ResumeJobRequest(reason="Retry after gateway outage"),
        "idempotency-key-001", AuthenticatedActor("operator-1", "kong_jwt"),
    )

    assert result["restart_mode"] == "FROM_CHECKPOINT"
    assert result["resume_from_stage"] == "ontology.admission"
    assert result["checkpoint_stage"] == "lane2.pass2"
    assert result["reconciliation_mode"] == "CONTINUE_FROM_CHECKPOINT"
    assert result["attempt"] == 2
    repository.claim_resume.assert_called_once_with(
        "run-1", idempotency_key="idempotency-key-001",
        requested_by="operator-1", reason="Retry after gateway outage",
        automatic_retry=False,
    )
    submitted = client.submit_job.call_args.kwargs
    assert submitted["submission_id"] == "resume-uuid"
    assert "--run-id run-1" in submitted["entrypoint"]
    assert "--job-id job-1" in submitted["entrypoint"]


@pytest.mark.asyncio
async def test_resume_api_repeat_returns_existing_attempt_without_ray_dispatch(monkeypatch):
    """Retrying the UI request after a lost response must not submit another Ray job."""
    repository = Mock()
    repository.get_resume_record.return_value = {
        "run_id": "run-1", "job_id": "job-1", "submitted_by": "operator-1",
        "status": "QUEUED",
    }
    repository.get_resume_attempt.return_value = {
        "attempt": 2, "ray_submission_id": "resume-1", "status": "QUEUED",
    }
    monkeypatch.setattr(api, "IngestionRunRepository", lambda: repository)
    client = Mock()
    monkeypatch.setattr(api, "get_job_client", lambda: client)

    result = await api.resume_ingestion_job(
        "job-1", None, "idempotency-key-004", AuthenticatedActor("operator-1", "kong_jwt"),
    )

    assert result["attempt"] == 2
    assert result["ray_submission_id"] == "resume-1"
    assert "already accepted" in result["message"]
    client.submit_job.assert_not_called()


@pytest.mark.asyncio
async def test_resume_api_accepts_report_only_checkpoint_after_store_success(monkeypatch, tmp_path):
    """The API permits a verified report checkpoint after publish and advertises REPORT_ONLY."""
    from kbfabric_ingest.ingest.checkpoints import stable_digest, write_checkpoint_artifact

    source_manifest = {"inventory_complete": True, "files": [{"name": "a.txt", "sha256": "abc"}]}
    run_context = {
        "customer_id": "customer-1", "tenant_id": "tenant-1", "industry_id": "banking",
        "domain_id": "cards", "product_line_id": "cards-pl",
        "customer_ontology_id": "ontology-1", "customer_ontology_version": 2,
        "canonical_ontology_id": "canonical-1", "canonical_ontology_version": "1",
        "extraction_policy_version": "2",
    }
    record = {
        "run_id": "run-1", "job_id": "job-1", "ingestion_batch_id": "batch-1",
        "submitted_by": "operator-1", "auth_source": "kong_jwt", "status": "FAILED",
        "customer_id": "customer-1", "tenant_id": "tenant-1", "industry_id": "banking",
        "domain_id": "cards", "product_line_id": "cards-pl", "customer_ontology_version": 2,
        "customer_ontology_id": "ontology-1", "canonical_ontology_id": "canonical-1",
        "canonical_ontology_version": "1", "extraction_policy_version": "2",
        "document_nature": "qe_assets", "hitl_mode": "review_ambiguous",
        "force_rewrite_requested": False, "run_context_json": run_context,
        "source_manifest_json": source_manifest, "stores_write_status": "SUCCEEDED",
    }
    artifact = tmp_path / "checkpoints" / "run-1" / "stores.write-attempt-1.json"
    digest = write_checkpoint_artifact(artifact, stage_id="stores.write", payload={"payload_kind": "report_inputs"})
    repository = Mock()
    repository.get_resume_record.return_value = record
    repository.get_resume_attempt.return_value = None
    repository.get_latest_checkpoint.return_value = {
        "stage_id": "stores.write", "artifact_uri": str(artifact), "artifact_sha256": digest,
        "source_manifest_sha256": stable_digest(source_manifest),
        "context_sha256": stable_digest(run_context),
    }
    repository.claim_resume.return_value = {
        "attempt": 2, "ray_submission_id": "resume-report", "status": "QUEUED",
        "already_claimed": False, "resume_from_stage": "run.verify_report",
        "checkpoint_stage": "stores.write", "checkpoint_id": "cp-report",
        "reconciliation_mode": "REPORT_ONLY",
    }
    client = Mock()
    client.list_jobs.return_value = []
    monkeypatch.setattr(api, "BASE_DIR", tmp_path)
    monkeypatch.setattr(api, "IngestionRunRepository", lambda: repository)
    monkeypatch.setattr(api, "_verify_resume_source_manifest", lambda _manifest: str(tmp_path))
    monkeypatch.setattr(api, "get_app_config", lambda: {"scheduler": {"max_concurrent_jobs": 10}})
    monkeypatch.setattr(api, "get_job_client", lambda: client)
    monkeypatch.setattr(api, "check_domain_lock", lambda *_args: (False, None))
    monkeypatch.setattr(api, "_resolve_ray_worker_dir", lambda _persist: "/ray")
    monkeypatch.setattr(api, "_get_ray_python_path", lambda _persist: "/venv/python")
    monkeypatch.setattr(api, "_get_ray_runtime_env", lambda *_a, **_kw: {"env_vars": {}})

    result = await api.resume_ingestion_job(
        "job-1", None, "report-only-key-1", AuthenticatedActor("operator-1", "kong_jwt"),
    )

    assert result["restart_mode"] == "REPORT_ONLY"
    assert result["resume_from_stage"] == "run.verify_report"
    assert result["reconciliation_mode"] == "REPORT_ONLY"
    repository.claim_resume.assert_called_once()
    client.submit_job.assert_called_once()


def test_job_details_and_event_stream_are_documented_as_authenticated_routes():
    """Objective: verify OpenAPI exposes authentication for durable job read operations.

    UI clients need the same bearer-token requirement in generated API docs that the
    route dependencies enforce at runtime.
    """
    schema = api.app.openapi()
    for path in ("/jobs/{job_id}", "/jobs/{job_id}/events"):
        operation = schema["paths"][path]["get"]
        authorization = [
            parameter for parameter in operation.get("parameters", [])
            if parameter.get("name") == "Authorization" and parameter.get("in") == "header"
        ]
        assert authorization

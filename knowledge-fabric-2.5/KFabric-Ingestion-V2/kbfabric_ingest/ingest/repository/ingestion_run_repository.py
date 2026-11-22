from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg

from kbfabric_ingest.utils.logger import get_logger


class IngestionRunRepository:
    """PostgreSQL source of truth for submitted ingestion-run audit context."""

    STAGES = (
        "run.queued", "run.validated", "context.frozen", "dependencies.ready",
        "documents.parsed", "blocks.prepared", "privacy.applied", "lane2.pass1",
        "lane2.pass2", "ontology.admission", "entity.mapping",
        "relationship.resolution", "admission.plan", "stores.write",
        "run.verify_report",
    )
    STAGE_STATUSES = {"PENDING", "RUNNING", "RETRY_WAIT", "SUCCEEDED", "SKIPPED", "FAILED"}

    def __init__(self) -> None:
        self.logger = get_logger(__name__)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ["POSTGRES_PORT"]),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
        )

    def create(self, record: dict[str, Any]) -> dict[str, str]:
        """Persist run audit data and return database-authoritative creation timestamps."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ingestion_run (
                        run_id, job_id, ingestion_batch_id, submitted_by, auth_source,
                        customer_id, tenant_id, industry_id, domain_id,
                        product_line_id, customer_ontology_id,
                        customer_ontology_version, canonical_ontology_id,
                        canonical_ontology_version, extraction_policy_version,
                        document_nature, hitl_mode, force_rewrite_requested,
                        run_context_json, submission_metadata_json, source_manifest_json
                    ) VALUES (
                        %(run_id)s, %(job_id)s, %(ingestion_batch_id)s, %(submitted_by)s, %(auth_source)s,
                        %(customer_id)s, %(tenant_id)s, %(industry_id)s, %(domain_id)s,
                        %(product_line_id)s, %(customer_ontology_id)s,
                        %(customer_ontology_version)s, %(canonical_ontology_id)s,
                        %(canonical_ontology_version)s, %(extraction_policy_version)s,
                        %(document_nature)s, %(hitl_mode)s, %(force_rewrite_requested)s,
                        %(run_context_json)s::jsonb, %(submission_metadata_json)s::jsonb,
                        %(source_manifest_json)s::jsonb
                    )
                    RETURNING submitted_at, created_at
                    """,
                    {
                        **record,
                        "run_context_json": json.dumps(record.get("run_context", {})),
                        "submission_metadata_json": json.dumps(record.get("submission_metadata", {})),
                        "source_manifest_json": json.dumps(record.get("source_manifest", {})),
                    },
                )
                timestamps = cur.fetchone()
                cur.executemany(
                    "INSERT INTO ingestion_run_stage (run_id, stage_id) VALUES (%s, %s)",
                    [(record["run_id"], stage_id) for stage_id in self.STAGES],
                )
                cur.execute(
                    "UPDATE ingestion_run_stage SET status = 'SUCCEEDED', completed_at = CURRENT_TIMESTAMP "
                    "WHERE run_id = %s AND stage_id IN ('run.validated', 'context.frozen')",
                    (record["run_id"],),
                )
                for stage_id in ("run.validated", "context.frozen"):
                    self._insert_event(
                        cur, record["run_id"], record["job_id"], "stage.succeeded",
                        stage_id=stage_id, status="SUCCEEDED",
                    )
                self._insert_event(
                    cur, record["run_id"], record["job_id"], "job.submitted", status="SUBMITTED",
                    payload={"submitted_by": record["submitted_by"], "auth_source": record["auth_source"]},
                )
        persisted_timestamps = {
            "submitted_at": timestamps[0].isoformat(),
            "created_at": timestamps[1].isoformat(),
        }
        self.logger.info(
            "ingestion_run.audit.created",
            run_id=record["run_id"],
            job_id=record["job_id"],
            submitted_by=record["submitted_by"],
            auth_source=record["auth_source"],
            **persisted_timestamps,
        )
        return persisted_timestamps

    def list_jobs(
        self, *, submitted_by: str | None = None, read_all: bool = False,
        limit: int = 50, offset: int = 0, status: str | None = None,
        customer_id: str | None = None, domain_id: str | None = None,
        product_line_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """List durable runs, scoped to the caller unless they have read_all access."""
        if not read_all and not submitted_by:
            return [], 0
        clauses: list[str] = []
        params: list[Any] = []
        if not read_all:
            clauses.append("submitted_by = %s")
            params.append(submitted_by)
        for column, value in (
            ("status", status), ("customer_id", customer_id),
            ("domain_id", domain_id), ("product_line_id", product_line_id),
        ):
            if value:
                clauses.append(f"{column} = %s")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(int(limit), 200))
        bounded_offset = max(0, int(offset))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM ingestion_run {where}", params)
                total = int(cur.fetchone()[0])
                cur.execute(
                    f"""SELECT job_id, run_id, ingestion_batch_id, submitted_by,
                               submitted_at, started_at, completed_at, updated_at, status,
                               customer_id, domain_id, product_line_id
                          FROM ingestion_run {where}
                         ORDER BY submitted_at DESC LIMIT %s OFFSET %s""",
                    [*params, bounded_limit, bounded_offset],
                )
                names = [column.name for column in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()], total

    def update_status(
        self, run_id: str, status: str, *, error_message: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> None:
        """Record worker lifecycle transitions without changing submitted identity."""
        normalized = status.upper()
        if normalized not in {
            "SUBMITTED", "QUEUED", "VALIDATING", "RUNNING", "RETRY_WAIT",
            "CANCEL_REQUESTED", "CANCELLED", "COMPLETED",
            "COMPLETED_WITH_REVIEW_PENDING", "SUCCEEDED", "FAILED",
        }:
            raise ValueError(f"Unsupported ingestion run status: {status}")
        with self._connect() as conn:
            with conn.cursor() as cur:
                if normalized == "COMPLETED":
                    cur.execute(
                        """SELECT EXISTS (
                               SELECT 1 FROM hitl_review h
                               JOIN ingestion_run r ON r.ingestion_batch_id = h.ingestion_batch_id
                              WHERE r.run_id = %s AND h.status = 'PENDING'
                           )""",
                        (run_id,),
                    )
                    if cur.fetchone()[0]:
                        normalized = "COMPLETED_WITH_REVIEW_PENDING"
                cur.execute(
                    """
                    UPDATE ingestion_run
                    SET status = %s,
                        started_at = CASE WHEN %s = 'RUNNING' THEN COALESCE(started_at, CURRENT_TIMESTAMP) ELSE started_at END,
                        completed_at = CASE WHEN %s IN ('SUCCEEDED', 'COMPLETED', 'COMPLETED_WITH_REVIEW_PENDING', 'CANCELLED', 'FAILED') THEN CURRENT_TIMESTAMP ELSE completed_at END,
                        error_message = %s,
                        error_json = COALESCE(%s::jsonb, error_json),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = %s
                    """,
                    (normalized, normalized, normalized, error_message,
                     json.dumps(error_details) if error_details else None, run_id),
                )
                if cur.rowcount != 1:
                    raise LookupError(f"Ingestion run not found: {run_id}")
                attempt_status = {
                    "RUNNING": "RUNNING", "COMPLETED": "COMPLETED",
                    "COMPLETED_WITH_REVIEW_PENDING": "COMPLETED", "SUCCEEDED": "COMPLETED",
                    "FAILED": "FAILED", "CANCELLED": "CANCELLED",
                }.get(normalized)
                if attempt_status:
                    cur.execute(
                        """UPDATE ingestion_run_attempt SET status = %s, updated_at = CURRENT_TIMESTAMP
                            WHERE run_id = %s AND attempt = (
                                SELECT max(attempt) FROM ingestion_run_attempt WHERE run_id = %s
                            )""",
                        (attempt_status, run_id, run_id),
                    )
                cur.execute("SELECT job_id FROM ingestion_run WHERE run_id = %s", (run_id,))
                job_id = cur.fetchone()[0]
                self._insert_event(
                    cur, run_id, job_id, "job.status_changed", status=normalized,
                    payload={
                        "error_present": bool(error_message),
                        "error_code": (error_details or {}).get("code"),
                        "retryable": (error_details or {}).get("retryable", False),
                    },
                )

    def list_auto_retry_candidates(self, *, max_attempts: int, limit: int = 50) -> list[dict[str, Any]]:
        """List due failed runs with a retryable error and remaining retry budget."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT r.job_id, r.run_id, r.submitted_by, r.auth_source, r.auto_retry_count
                         FROM ingestion_run r
                         JOIN ingestion_run_stage store_stage
                           ON store_stage.run_id = r.run_id AND store_stage.stage_id = 'stores.write'
                        WHERE r.status = 'FAILED'
                          AND COALESCE((r.error_json->>'retryable')::boolean, FALSE) = TRUE
                          AND r.auto_retry_count < %s
                          AND store_stage.status <> 'SUCCEEDED'
                          AND EXISTS (
                              SELECT 1 FROM ingestion_run_checkpoint cp
                               WHERE cp.run_id = r.run_id
                                 AND cp.stage_id IN ('blocks.prepared', 'lane2.pass2', 'admission.plan', 'stores.write')
                                 AND (store_stage.status = 'PENDING'
                                      OR cp.stage_id = 'admission.plan'
                                      OR (cp.stage_id = 'stores.write' AND store_stage.status = 'SUCCEEDED'))
                          )
                          AND r.updated_at + make_interval(secs => LEAST(60, 5 * power(2, r.auto_retry_count))) <= CURRENT_TIMESTAMP
                        ORDER BY r.updated_at ASC LIMIT %s""",
                    (max(0, int(max_attempts)), max(1, min(int(limit), 100))),
                )
                names = [column.name for column in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]

    def suppress_auto_retry(self, run_id: str, *, reason: str) -> None:
        """Stop automatic polling after a permanent checkpoint/context rejection."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE ingestion_run
                          SET error_json = COALESCE(error_json, '{}'::jsonb) ||
                              jsonb_build_object('retryable', false, 'auto_retry_suppressed_reason', %s),
                              updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = %s AND status = 'FAILED'""",
                    (reason[:500], run_id),
                )

    def request_cancel(self, job_id: str, *, requested_by: str) -> dict[str, Any]:
        """Atomically request cancellation only before the publishing stage starts."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT run_id, submitted_by, status FROM ingestion_run WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                )
                run = cur.fetchone()
                if run is None:
                    raise LookupError(f"Ingestion job not found: {job_id}")
                run_id, submitted_by, status = run
                cur.execute(
                    "SELECT status FROM ingestion_run_stage WHERE run_id = %s AND stage_id = 'stores.write'",
                    (run_id,),
                )
                store = cur.fetchone()
                if store is None:
                    raise LookupError(f"Publishing stage missing for run: {run_id}")
                if status in {"CANCEL_REQUESTED", "CANCELLED"}:
                    return {"run_id": run_id, "status": status, "already_requested": True}
                if status not in {"SUBMITTED", "QUEUED", "VALIDATING", "RUNNING", "RETRY_WAIT"} or store[0] != "PENDING":
                    raise ValueError("Cancellation is unavailable after publishing starts or the run becomes terminal")
                cur.execute(
                    "UPDATE ingestion_run SET status = 'CANCEL_REQUESTED', updated_at = CURRENT_TIMESTAMP WHERE run_id = %s",
                    (run_id,),
                )
                self._insert_event(
                    cur, run_id, job_id, "job.cancel_requested", status="CANCEL_REQUESTED",
                    payload={"requested_by": requested_by},
                )
                return {"run_id": run_id, "status": "CANCEL_REQUESTED", "already_requested": False,
                        "submitted_by": submitted_by}

    def is_cancel_requested(self, run_id: str) -> bool:
        """Read the durable cooperative-cancel flag."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status = 'CANCEL_REQUESTED' FROM ingestion_run WHERE run_id = %s", (run_id,))
                row = cur.fetchone()
                if row is None:
                    raise LookupError(f"Ingestion run not found: {run_id}")
                return bool(row[0])

    def start_worker(self, run_id: str) -> bool:
        """Mark the worker active unless a queued cancellation already won."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT job_id, status FROM ingestion_run WHERE run_id = %s FOR UPDATE", (run_id,))
                row = cur.fetchone()
                if row is None:
                    raise LookupError(f"Ingestion run not found: {run_id}")
                if row[1] == "CANCEL_REQUESTED":
                    return False
                cur.execute(
                    "UPDATE ingestion_run SET status = 'RUNNING', started_at = COALESCE(started_at, CURRENT_TIMESTAMP), updated_at = CURRENT_TIMESTAMP WHERE run_id = %s",
                    (run_id,),
                )
                cur.execute(
                    "UPDATE ingestion_run_stage SET status = 'SUCCEEDED', completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                    "WHERE run_id = %s AND stage_id = 'run.queued' AND status IN ('PENDING', 'RUNNING')",
                    (run_id,),
                )
                if cur.rowcount:
                    self._insert_event(cur, run_id, row[0], "stage.succeeded",
                                       stage_id="run.queued", status="SUCCEEDED")
                cur.execute(
                    """UPDATE ingestion_run_attempt SET status = 'RUNNING', updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = %s AND attempt = (
                            SELECT MAX(attempt) FROM ingestion_run_attempt WHERE run_id = %s
                        )""",
                    (run_id, run_id),
                )
                self._insert_event(cur, run_id, row[0], "job.status_changed", status="RUNNING")
                return True

    def begin_store_publishing(self, run_id: str) -> bool:
        """Atomically win the cancel-versus-publish boundary; False means cancel won."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT job_id, status FROM ingestion_run WHERE run_id = %s FOR UPDATE", (run_id,))
                run = cur.fetchone()
                if run is None:
                    raise LookupError(f"Ingestion run not found: {run_id}")
                if run[1] == "CANCEL_REQUESTED":
                    return False
                cur.execute(
                    "UPDATE ingestion_run_stage SET status = 'RUNNING', started_at = COALESCE(started_at, CURRENT_TIMESTAMP), updated_at = CURRENT_TIMESTAMP WHERE run_id = %s AND stage_id = 'stores.write' AND status = 'PENDING'",
                    (run_id,),
                )
                if cur.rowcount != 1:
                    raise ValueError("Publishing stage is not pending")
                self._insert_event(cur, run_id, run[0], "stage.running", stage_id="stores.write", status="RUNNING")
                return True

    def record_stage(
        self,
        run_id: str,
        stage_id: str,
        status: str,
        *,
        progress: dict[str, Any] | None = None,
        skip_reason: str | None = None,
        error: dict[str, Any] | None = None,
        attempt: int | None = None,
    ) -> None:
        """Persist one stage transition and its ordered event atomically."""
        normalized = status.upper()
        if stage_id not in self.STAGES:
            raise ValueError(f"Unknown ingestion stage: {stage_id}")
        if normalized not in self.STAGE_STATUSES:
            raise ValueError(f"Unsupported ingestion stage status: {status}")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ingestion_run_stage
                    SET status = %s,
                        attempt = COALESCE(
                            %s,
                            CASE WHEN %s = 'RUNNING' AND status IN ('FAILED', 'RETRY_WAIT', 'SUCCEEDED', 'SKIPPED')
                                 THEN attempt + 1 ELSE attempt END
                        ),
                        started_at = CASE
                            WHEN %s = 'RUNNING' AND status <> 'RUNNING' THEN CURRENT_TIMESTAMP
                            ELSE started_at
                        END,
                        completed_at = CASE WHEN %s IN ('SUCCEEDED', 'SKIPPED', 'FAILED') THEN CURRENT_TIMESTAMP ELSE NULL END,
                        updated_at = CURRENT_TIMESTAMP,
                        skip_reason = %s,
                        progress_json = COALESCE(%s::jsonb, progress_json),
                        error_json = %s::jsonb
                    WHERE run_id = %s AND stage_id = %s
                    """,
                    (
                        normalized, attempt, normalized, normalized, normalized, skip_reason,
                        json.dumps(progress) if progress is not None else None,
                        json.dumps(error) if error else None,
                        run_id, stage_id,
                    ),
                )
                if cur.rowcount != 1:
                    raise LookupError(f"Ingestion stage not found: {run_id}/{stage_id}")
                cur.execute("SELECT job_id FROM ingestion_run WHERE run_id = %s", (run_id,))
                job_id = cur.fetchone()[0]
                self._insert_event(
                    cur, run_id, job_id, f"stage.{normalized.lower()}",
                    stage_id=stage_id, status=normalized,
                    payload={
                        "progress": progress or {},
                        "skip_reason": skip_reason,
                        "error_present": bool(error),
                        "error_code": (error or {}).get("code"),
                    },
                )

    def record_progress(self, run_id: str, phase: str, progress: dict[str, Any]) -> None:
        """Persist a safe phase/count snapshot as an ordered job event."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT job_id FROM ingestion_run WHERE run_id = %s", (run_id,))
                row = cur.fetchone()
                if row is None:
                    raise LookupError(f"Ingestion run not found: {run_id}")
                self._insert_event(
                    cur, run_id, row[0], "job.progress", status="RUNNING",
                    payload={"phase": phase, "progress": progress},
                )

    def commit_checkpoint(
        self,
        run_id: str,
        stage_id: str,
        *,
        attempt: int,
        unit_key: str,
        artifact_uri: str,
        artifact_sha256: str,
        source_manifest_sha256: str,
        context_sha256: str,
        cursor: dict[str, Any] | None = None,
    ) -> str:
        """Record a pointer to a retained checkpoint artifact and its provenance.

        This ledger does not itself implement restore or imply resume eligibility.
        The caller must upload/commit the immutable artifact before calling this method.
        """
        if stage_id not in self.STAGES:
            raise ValueError(f"Unknown ingestion stage: {stage_id}")
        if attempt < 1 or not unit_key.strip() or not artifact_uri.strip():
            raise ValueError("Checkpoint attempt, unit key, and artifact URI are required")
        for name, digest in (
            ("artifact_sha256", artifact_sha256),
            ("source_manifest_sha256", source_manifest_sha256),
            ("context_sha256", context_sha256),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")

        checkpoint_id = str(uuid.uuid4())
        with self._connect() as conn:
            with conn.cursor() as cur:
                # A business stage can be re-entered within one worker execution
                # (for example, split Lane 2 passes). The durable stage attempt,
                # not the whole-run execution attempt, identifies this checkpoint.
                cur.execute(
                    "SELECT attempt FROM ingestion_run_stage WHERE run_id = %s AND stage_id = %s FOR UPDATE",
                    (run_id, stage_id),
                )
                stage_attempt_row = cur.fetchone()
                if stage_attempt_row is None:
                    raise LookupError(f"Ingestion stage not found: {run_id}/{stage_id}")
                stage_attempt = int(stage_attempt_row[0])
                cur.execute(
                    """INSERT INTO ingestion_run_checkpoint (
                           checkpoint_id, run_id, stage_id, attempt, unit_key, artifact_uri,
                           artifact_sha256, source_manifest_sha256, context_sha256, cursor_json
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
                    (
                        checkpoint_id, run_id, stage_id, stage_attempt, unit_key, artifact_uri,
                        artifact_sha256, source_manifest_sha256, context_sha256,
                        json.dumps(cursor or {}),
                    ),
                )
                cur.execute(
                    "UPDATE ingestion_run_stage SET checkpoint_id = %s, updated_at = CURRENT_TIMESTAMP "
                    "WHERE run_id = %s AND stage_id = %s AND attempt = %s",
                    (checkpoint_id, run_id, stage_id, stage_attempt),
                )
                if cur.rowcount != 1:
                    raise LookupError(
                        f"Ingestion stage attempt not found: {run_id}/{stage_id}/{stage_attempt}"
                    )
                cur.execute("SELECT job_id FROM ingestion_run WHERE run_id = %s", (run_id,))
                row = cur.fetchone()
                if row is None:
                    raise LookupError(f"Ingestion run not found: {run_id}")
                self._insert_event(
                    cur, run_id, row[0], "checkpoint.committed", stage_id=stage_id,
                    status="SUCCEEDED", payload={"checkpoint_id": checkpoint_id, "unit_key": unit_key},
                )
        return checkpoint_id

    def record_store_write_unit(
        self, run_id: str, store_name: str, unit_key: str, status: str, *,
        attempt: int, progress: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Persist one idempotent store unit outcome before advancing the worker."""
        store_name = store_name.lower()
        status = status.upper()
        if store_name not in {"weaviate", "neo4j"}:
            raise ValueError("Unsupported knowledge store")
        if status not in {"IN_PROGRESS", "SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise ValueError("Unsupported store write status")
        if not unit_key or len(unit_key) > 512:
            raise ValueError("Store write unit key is invalid")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO ingestion_run_store_write (
                           run_id, store_name, unit_key, status, attempt,
                           progress_json, error_json, started_at, updated_at, completed_at
                       ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                                 CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                                 CASE WHEN %s IN ('SUCCEEDED','FAILED','UNKNOWN') THEN CURRENT_TIMESTAMP END)
                       ON CONFLICT (run_id, store_name, unit_key) DO UPDATE SET
                           status = EXCLUDED.status,
                           attempt = EXCLUDED.attempt,
                           progress_json = EXCLUDED.progress_json,
                           error_json = EXCLUDED.error_json,
                           started_at = CASE WHEN EXCLUDED.status = 'IN_PROGRESS'
                                             THEN CURRENT_TIMESTAMP
                                             ELSE ingestion_run_store_write.started_at END,
                           updated_at = CURRENT_TIMESTAMP,
                           completed_at = CASE WHEN EXCLUDED.status IN ('SUCCEEDED','FAILED','UNKNOWN')
                                               THEN CURRENT_TIMESTAMP ELSE NULL END""",
                    (run_id, store_name, unit_key, status, max(1, int(attempt)),
                     json.dumps(progress or {}), json.dumps(error) if error else None, status),
                )
                cur.execute("SELECT job_id FROM ingestion_run WHERE run_id = %s", (run_id,))
                run = cur.fetchone()
                if run:
                    self._insert_event(
                        cur, run_id, run[0], "store.write_status_changed",
                        stage_id="stores.write", status=status,
                        payload={"store": store_name, "unit_key": unit_key,
                                 "attempt": max(1, int(attempt)),
                                 "progress": progress or {},
                                 "error_code": (error or {}).get("code")},
                    )

    def list_store_write_units(self, run_id: str) -> list[dict[str, Any]]:
        """Return per-store reconciliation state for the job details view."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT store_name, unit_key, status, attempt, progress_json,
                              error_json, started_at, updated_at, completed_at
                         FROM ingestion_run_store_write WHERE run_id = %s
                        ORDER BY store_name, unit_key""",
                    (run_id,),
                )
                names = [column.name for column in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]

    def get_latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        """Return the latest checkpoint reference; callers must verify artifact and digests."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT checkpoint_id, run_id, stage_id, attempt, unit_key, artifact_uri,
                              artifact_sha256, source_manifest_sha256, context_sha256,
                              cursor_json, created_at
                         FROM ingestion_run_checkpoint
                        WHERE run_id = %s
                        ORDER BY created_at DESC, checkpoint_id DESC
                        LIMIT 1""",
                    (run_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                names = [column.name for column in cur.description]
                return dict(zip(names, row))

    def get_resume_record(self, job_id: str) -> dict[str, Any] | None:
        """Load the immutable submission data needed to validate a whole-run restart."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT r.run_id, r.job_id, r.ingestion_batch_id, r.submitted_by,
                              r.auth_source, r.status, r.customer_id, r.tenant_id,
                              r.industry_id, r.domain_id, r.product_line_id,
                              r.customer_ontology_version, r.document_nature, r.hitl_mode,
                              r.force_rewrite_requested, r.run_context_json,
                              r.submission_metadata_json, r.source_manifest_json,
                              s.status AS stores_write_status, r.error_json, r.auto_retry_count
                         FROM ingestion_run r
                         JOIN ingestion_run_stage s ON s.run_id = r.run_id
                                                   AND s.stage_id = 'stores.write'
                        WHERE r.job_id = %s""",
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                names = [column.name for column in cur.description]
                return dict(zip(names, row))

    def get_resume_attempt(self, run_id: str, idempotency_key: str) -> dict[str, Any] | None:
        """Return a prior accepted attempt for an idempotent API retry."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT attempt, ray_submission_id, status FROM ingestion_run_attempt
                        WHERE run_id = %s AND idempotency_key = %s""",
                    (run_id, idempotency_key),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {"attempt": int(row[0]), "ray_submission_id": row[1], "status": row[2]}

    def claim_resume(
        self, run_id: str, *, idempotency_key: str, requested_by: str,
        reason: str | None = None, automatic_retry: bool = False,
    ) -> dict[str, Any]:
        """Claim a run and preserve completed stages through its latest safe checkpoint."""
        if not idempotency_key or len(idempotency_key) < 8:
            raise ValueError("Idempotency-Key must contain at least 8 characters")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM ingestion_run WHERE run_id = %s FOR UPDATE",
                    (run_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise LookupError(f"Ingestion run not found: {run_id}")
                cur.execute(
                    """SELECT attempt, ray_submission_id, status
                         FROM ingestion_run_attempt
                        WHERE run_id = %s AND idempotency_key = %s""",
                    (run_id, idempotency_key),
                )
                existing = cur.fetchone()
                if existing is not None:
                    return {
                        "attempt": int(existing[0]),
                        "ray_submission_id": existing[1],
                        "status": existing[2],
                        "already_claimed": True,
                    }
                if row[0] not in {"FAILED", "CANCELLED"}:
                    raise ValueError("Only failed or cancelled runs can be resumed")

                cur.execute(
                    """SELECT checkpoint_id, stage_id, artifact_uri, artifact_sha256,
                              source_manifest_sha256, context_sha256
                         FROM ingestion_run_checkpoint WHERE run_id = %s
                         ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1""",
                    (run_id,),
                )
                checkpoint = cur.fetchone()
                if checkpoint is None:
                    raise ValueError("Run has no durable stage checkpoint and cannot be resumed")
                checkpoint_stage = checkpoint[1]
                if checkpoint_stage not in {"blocks.prepared", "lane2.pass2", "admission.plan", "stores.write"}:
                    raise ValueError("Latest checkpoint is not a supported recovery boundary")
                checkpoint_index = self.STAGES.index(checkpoint_stage)
                cur.execute(
                    "SELECT status FROM ingestion_run_stage WHERE run_id = %s AND stage_id = 'stores.write'",
                    (run_id,),
                )
                store_stage = cur.fetchone()
                if store_stage is None:
                    raise ValueError("Store publishing stage is missing")
                report_only_recovery = checkpoint_stage == "stores.write" and store_stage[0] == "SUCCEEDED"
                if store_stage[0] == "SUCCEEDED" and not report_only_recovery:
                    raise ValueError("Store publishing succeeded without a report recovery checkpoint")
                if store_stage[0] != "PENDING" and checkpoint_stage not in {"admission.plan", "stores.write"}:
                    raise ValueError("Partial publishing has no admission checkpoint for safe replay")

                cur.execute(
                    "SELECT COALESCE(MAX(attempt), 1) + 1 FROM ingestion_run_attempt WHERE run_id = %s",
                    (run_id,),
                )
                attempt = int(cur.fetchone()[0])
                ray_submission_id = f"resume-{uuid.uuid4()}"
                cur.execute(
                    """INSERT INTO ingestion_run_attempt (
                           run_id, attempt, idempotency_key, ray_submission_id, requested_by, reason
                       ) VALUES (%s, %s, %s, %s, %s, %s)""",
                    (run_id, attempt, idempotency_key, ray_submission_id, requested_by, reason),
                )
                cur.execute(
                    """UPDATE ingestion_run SET status = 'QUEUED', completed_at = NULL,
                              error_message = NULL,
                              auto_retry_count = auto_retry_count + %s,
                              updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = %s""",
                    (1 if automatic_retry else 0, run_id),
                )
                for index, stage_id in enumerate(self.STAGES):
                    if index <= checkpoint_index:
                        # Keep the checkpoint and its successful stage history intact.
                        continue
                    cur.execute(
                        """UPDATE ingestion_run_stage
                              SET status = 'PENDING', attempt = attempt + 1,
                                  started_at = NULL, completed_at = NULL, skip_reason = NULL,
                                  progress_json = '{}'::jsonb, checkpoint_id = NULL,
                                  error_json = NULL, updated_at = CURRENT_TIMESTAMP
                            WHERE run_id = %s AND stage_id = %s""",
                        (run_id, stage_id),
                    )
                cur.execute("SELECT job_id FROM ingestion_run WHERE run_id = %s", (run_id,))
                job_id = cur.fetchone()[0]
                self._insert_event(
                    cur, run_id, job_id, "job.resume_requested", status="QUEUED",
                    stage_id=checkpoint_stage,
                    payload={"attempt": attempt, "requested_by": requested_by,
                             "resume_from_stage": checkpoint_stage,
                             "checkpoint_id": str(checkpoint[0])},
                )
                return {
                    "attempt": attempt, "ray_submission_id": ray_submission_id,
                    "status": "QUEUED", "already_claimed": False,
                    "checkpoint_id": str(checkpoint[0]), "checkpoint_stage": checkpoint_stage,
                    "resume_from_stage": self.STAGES[checkpoint_index + 1],
                    "reconciliation_mode": (
                        "REPORT_ONLY" if report_only_recovery else
                        "IDEMPOTENT_STORE_REPLAY" if store_stage[0] != "PENDING"
                        else "CONTINUE_FROM_CHECKPOINT"
                    ),
                }

    def get_job_details(self, job_id: str) -> dict[str, Any] | None:
        """Load the durable run snapshot and stable ordered stage timeline."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT run_id, job_id, ingestion_batch_id, submitted_by, auth_source,
                              submitted_at, started_at, completed_at, updated_at, status,
                              customer_id, tenant_id, industry_id, domain_id, product_line_id,
                              customer_ontology_id, customer_ontology_version,
                              canonical_ontology_id, canonical_ontology_version,
                              extraction_policy_version, document_nature, hitl_mode,
                              force_rewrite_requested, error_message, error_json, auto_retry_count, run_context_json,
                              submission_metadata_json,
                              COALESCE((SELECT MAX(a.attempt) FROM ingestion_run_attempt a
                                         WHERE a.run_id = ingestion_run.run_id), 1) AS execution_attempt
                       FROM ingestion_run WHERE job_id = %s""",
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                names = [column.name for column in cur.description]
                details = dict(zip(names, row))
                failure = details.get("error_json") or {}
                details["error"] = failure or None
                from kbfabric_ingest.ingest.retry_policy import (
                    automatic_retry_enabled, automatic_retry_max_attempts,
                )
                details["automatic_retry_pending"] = bool(
                    details.get("status") == "FAILED"
                    and failure.get("retryable") is True
                    and automatic_retry_enabled()
                    and int(details.get("auto_retry_count", 0)) < automatic_retry_max_attempts()
                )
                metadata = details.pop("submission_metadata_json") or {}
                details["project_ids"] = metadata.get("project_ids", [])
                details["context_tags"] = metadata.get("context_tags", [])
                cur.execute(
                    """SELECT stage_id, status, attempt, started_at, updated_at, completed_at,
                              skip_reason, progress_json, checkpoint_id, error_json
                       FROM ingestion_run_stage WHERE run_id = %s""",
                    (details["run_id"],),
                )
                stage_names = [column.name for column in cur.description]
                stages = {item[0]: dict(zip(stage_names, item)) for item in cur.fetchall()}
                details["stages"] = [
                    stages.get(stage_id, {"stage_id": stage_id, "status": "PENDING", "attempt": 1})
                    for stage_id in self.STAGES
                ]
                cur.execute(
                    """SELECT store_name, unit_key, status, attempt, progress_json,
                              error_json, started_at, updated_at, completed_at
                         FROM ingestion_run_store_write WHERE run_id = %s
                        ORDER BY store_name, unit_key""",
                    (details["run_id"],),
                )
                write_names = [column.name for column in cur.description]
                details["store_write_units"] = [dict(zip(write_names, item)) for item in cur.fetchall()]
                details["store_reconciliation_required"] = any(
                    item["status"] in {"IN_PROGRESS", "FAILED", "UNKNOWN"}
                    for item in details["store_write_units"]
                ) or (
                    stages.get("stores.write", {}).get("status") == "FAILED"
                    and bool(details["store_write_units"])
                )
                now = datetime.now(timezone.utc)
                for stage in details["stages"]:
                    started_at = stage.get("started_at")
                    if started_at is None:
                        stage["duration_ms"] = None
                    else:
                        ended_at = stage.get("completed_at") or now
                        stage["duration_ms"] = max(
                            0, int((ended_at - started_at).total_seconds() * 1000)
                        )
                details["current_stage_id"] = next(
                    (stage["stage_id"] for stage in details["stages"] if stage["status"] == "RUNNING"),
                    None,
                )
                cur.execute(
                    """SELECT
                           count(*) FILTER (WHERE status = 'PENDING') AS pending_decisions,
                           count(*) FILTER (WHERE status = 'APPROVED') AS approved_decisions,
                           count(*) FILTER (WHERE status = 'REJECTED') AS rejected_decisions,
                           count(*) FILTER (WHERE write_status = 'PENDING') AS pending_writes,
                           count(*) FILTER (WHERE write_status = 'FAILED') AS failed_writes,
                           count(*) FILTER (WHERE write_status = 'SUCCEEDED') AS succeeded_writes
                       FROM hitl_review
                       WHERE ingestion_batch_id = %s
                         AND ontology_context_json->>'domain_id' = %s
                         AND ontology_context_json->>'product_line_id' = %s""",
                    (details["ingestion_batch_id"], details["domain_id"], details["product_line_id"]),
                )
                hitl_row = cur.fetchone()
                hitl_names = [column.name for column in cur.description]
                details["hitl_summary"] = dict(zip(hitl_names, hitl_row))
                details["pending_review_count"] = details["hitl_summary"]["pending_decisions"]
                cur.execute(
                    """SELECT r.status, s.status, r.source_manifest_json, r.run_context_json,
                              (SELECT cp.stage_id FROM ingestion_run_checkpoint cp
                                WHERE cp.run_id = r.run_id
                                ORDER BY cp.created_at DESC, cp.checkpoint_id DESC LIMIT 1)
                         FROM ingestion_run r
                         JOIN ingestion_run_stage s ON s.run_id = r.run_id
                                                   AND s.stage_id = 'stores.write'
                        WHERE r.run_id = %s""",
                    (details["run_id"],),
                )
                resume_state = cur.fetchone()
                manifest = resume_state[2] if resume_state else {}
                frozen_context = resume_state[3] if resume_state else {}
                checkpoint_stage = resume_state[4] if resume_state else None
                context_complete = isinstance(frozen_context, dict) and all(
                    key in frozen_context for key in (
                        "customer_id", "tenant_id", "industry_id", "domain_id",
                        "product_line_id", "customer_ontology_id",
                        "customer_ontology_version", "canonical_ontology_id",
                        "canonical_ontology_version", "extraction_policy_version",
                    )
                )
                details["resume_available"] = bool(
                    resume_state
                    and resume_state[0] in {"FAILED", "CANCELLED"}
                    and (
                        resume_state[1] == "PENDING"
                        or (resume_state[1] in {"FAILED", "RUNNING"}
                            and checkpoint_stage == "admission.plan")
                        or (resume_state[1] == "SUCCEEDED"
                            and checkpoint_stage == "stores.write")
                    )
                    and checkpoint_stage in {"blocks.prepared", "lane2.pass2", "admission.plan", "stores.write"}
                    and manifest.get("inventory_complete")
                    and manifest.get("files")
                    and context_complete
                )
                details["resume_mode"] = (
                    "REPORT_ONLY" if details["resume_available"] and checkpoint_stage == "stores.write"
                    else "FROM_CHECKPOINT" if details["resume_available"] else None
                )
                details["cancel_available"] = bool(
                    details.get("status") in {"SUBMITTED", "QUEUED", "VALIDATING", "RUNNING", "RETRY_WAIT"}
                    and stages.get("stores.write", {}).get("status") == "PENDING"
                )
                return details

    def resolve_event_cursor(self, job_id: str, event_id: str) -> int:
        """Resolve a replay cursor only when it belongs to this job."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_sequence FROM ingestion_run_event WHERE job_id = %s AND event_id::text = %s",
                    (job_id, event_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError("Last-Event-ID is unknown for this job")
                return int(row[0])

    def list_events_after(self, job_id: str, sequence: int, limit: int = 100) -> list[dict[str, Any]]:
        """Read ordered persisted events after a sequence cursor."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT event_sequence, event_id::text, run_id, job_id, event_type,
                              stage_id, status, payload_json, occurred_at
                       FROM ingestion_run_event WHERE job_id = %s AND event_sequence > %s
                       ORDER BY event_sequence ASC LIMIT %s""",
                    (job_id, sequence, max(1, min(int(limit), 500))),
                )
                names = [column.name for column in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]

    def skip_pending_stages(self, run_id: str, reason: str) -> int:
        """Close untouched stages as skipped when a run completes successfully."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE ingestion_run_stage SET status = 'SKIPPED', skip_reason = %s,
                              completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                       WHERE run_id = %s AND status = 'PENDING' RETURNING stage_id""",
                    (reason, run_id),
                )
                skipped = [row[0] for row in cur.fetchall()]
                cur.execute("SELECT job_id FROM ingestion_run WHERE run_id = %s", (run_id,))
                row = cur.fetchone()
                if row is None:
                    raise LookupError(f"Ingestion run not found: {run_id}")
                for stage_id in skipped:
                    self._insert_event(
                        cur, run_id, row[0], "stage.skipped", stage_id=stage_id,
                        status="SKIPPED", payload={"skip_reason": reason},
                    )
                return len(skipped)

    @staticmethod
    def _insert_event(
        cursor: Any,
        run_id: str,
        job_id: str,
        event_type: str,
        *,
        stage_id: str | None = None,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO ingestion_run_event (
                event_id, run_id, job_id, event_type, stage_id, status, payload_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (str(uuid.uuid4()), run_id, job_id, event_type, stage_id, status, json.dumps(payload or {})),
        )

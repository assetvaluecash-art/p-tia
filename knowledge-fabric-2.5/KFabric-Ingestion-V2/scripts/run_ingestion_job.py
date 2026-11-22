#!/usr/bin/env python
"""
Ray Jobs Entry Point - Standalone Ingestion Script

This script is executed by Ray Jobs for each ingestion request.
It runs independently and writes progress to a job-specific file.

Usage (by Ray Jobs):
    python scripts/run_ingestion_job.py \
        --job-id ingest-wp-001 \
        --domain wealth_portfolio \
        --input-dir drop_zone/wealth_portfolio \
        --force-rewrite

Output:
    - Progress file: progress/{job_id}.json
    - Reports: reports/semantic_report_{timestamp}.html, etc.
    - Logs: stdout captured by Ray Dashboard
"""

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Add parent to path for imports
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(BASE_DIR))

def _resolve_persist_dir(input_dir: str) -> Path:
    """
    Resolve a REAL filesystem directory for persisting progress/reports.
    Ray Jobs copies working_dir into a temp package; we must write outside that temp dir.

    Priority:
    1) KBFABRIC_INGEST_PERSIST_DIR env var (set by scheduler)
    2) Derive from input_dir (/.../kbfabric-ingest/drop_zone/<domain> -> /.../kbfabric-ingest)
    3) Fallback to BASE_DIR (non-Ray local runs)
    """
    import os

    env_dir = (os.environ.get("KBFABRIC_INGEST_PERSIST_DIR") or "").strip()
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if p.exists():
            return p

    try:
        ip = Path(input_dir).expanduser().resolve()
        # If input_dir is .../kbfabric-ingest/drop_zone/<domain>, then parents[1] is kbfabric-ingest
        if ip.exists() and ip.parent.name == "drop_zone":
            return ip.parent.parent
        if ip.exists() and ip.parent.parent.name == "drop_zone":
            return ip.parent.parent.parent
    except Exception:
        pass

    return BASE_DIR


def _job_report_base(job_id: str) -> str:
    """Normalize job id to a consistent report/log base name."""
    return job_id if job_id.startswith("ingest-") else f"ingest-{job_id}"


def _should_apply_force_rewrite(force_rewrite: bool, execution_attempt: int) -> bool:
    """Product-line cleanup is a one-time first-attempt action, never a resume action."""
    return bool(force_rewrite) and int(execution_attempt or 1) <= 1


def _metadata_values_as_raw(value) -> str | None:
    """Convert persisted submission metadata into the ingestion service's raw format."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, tuple, set)):
        values = [str(item).strip() for item in value if item is not None and str(item).strip()]
        return ";".join(values) or None
    return str(value)


class _TeeStream:
    """Write to both a file and the original stream."""

    def __init__(self, original, file_handle):
        self._original = original
        self._file_handle = file_handle

    def write(self, text):
        self._original.write(text)
        self._file_handle.write(text)

    def flush(self):
        self._original.flush()
        self._file_handle.flush()

    def isatty(self):
        return self._original.isatty()

class JobProgressTracker:
    """Write progress to job-specific file and stdout."""
    
    def __init__(self, job_id: str, domain: str, input_dir: str, progress_dir: Path, product_line_id: str | None = None, document_nature: str = "qe_assets", run_id: str | None = None, submitted_by: str | None = None, auth_source: str | None = None, customer_id: str | None = None, customer_ontology_version: int | None = None, audit_context: dict | None = None):
        self.job_id = job_id
        self.domain = domain
        self.input_dir = input_dir
        self.progress_dir = progress_dir
        self.progress_dir.mkdir(parents=True, exist_ok=True)
        self.progress_file = self.progress_dir / f"{job_id}.json"
        self.start_time = datetime.now(timezone.utc)
        self._active_persisted_stage: str | None = None
        self._active_persisted_stages: set[str] = set()
        self._run_repository = None
        
        self.state = {
            "job_id": job_id,
            "run_id": run_id,
            "submitted_by": submitted_by,
            "auth_source": auth_source,
            "customer_id": customer_id,
            "customer_ontology_version": customer_ontology_version,
            "audit_context": audit_context or {},
            "tenant_id": (audit_context or {}).get("tenant_id"),
            "industry_id": (audit_context or {}).get("industry_id"),
            "customer_ontology_id": (audit_context or {}).get("customer_ontology_id"),
            "canonical_ontology_id": (audit_context or {}).get("canonical_ontology_id"),
            "canonical_ontology_version": (audit_context or {}).get("canonical_ontology_version"),
            "extraction_policy_version": (audit_context or {}).get("extraction_policy_version"),
            "submitted_at": (audit_context or {}).get("submitted_at"),
            "created_at": (audit_context or {}).get("created_at"),
            "domain": domain,
            "product_line_id": product_line_id,
            "document_nature": document_nature,
            "input_dir": input_dir,
            "status": "starting",
            "phase": "Initializing",
            "phase_number": 0,
            "total_phases": 8,
            "progress_pct": 0,
            "files_done": 0,
            "files_total": 0,
            "current_file": "",
            "blocks_extracted": 0,
            "entities_found": 0,
            "relationships_found": 0,
            "images_processed": 0,
            "weaviate_chunks": 0,
            "neo4j_nodes": 0,
            "elapsed_sec": 0,
            "started_at": self.start_time.isoformat(),
            "updated_at": self.start_time.isoformat(),
            "error": None,
            "report_paths": {}
            ,
            # Non-fatal warnings (e.g., report generation issues) for UI clarity
            "warning_count": 0
        }
        self._write()
    
    def _write(self):
        """Write current state to file and log."""
        self.state["elapsed_sec"] = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        with open(self.progress_file, "w") as f:
            json.dump(self.state, f, indent=2)
        
        # Also print for Ray Dashboard logs
        print(f"[PROGRESS] {self.state['phase']} | {self.state['progress_pct']}% | "
              f"Files: {self.state['files_done']}/{self.state['files_total']} | "
              f"Blocks: {self.state['blocks_extracted']} | Entities: {self.state['entities_found']}", 
              flush=True)
    
    def update(self, **kwargs):
        """Update progress state."""
        self.state.update(kwargs)
        self._write()
    
    def set_phase(self, phase: str, phase_number: int, progress_pct: int):
        """Set current phase and persist a safe, ordered progress event."""
        self.state["phase"] = phase
        self.state["phase_number"] = phase_number
        self.state["progress_pct"] = progress_pct
        self._persist_phase(phase, progress_pct)
        self._write()

    def _repository(self):
        if self._run_repository is None:
            from kbfabric_ingest.ingest.repository.ingestion_run_repository import IngestionRunRepository
            self._run_repository = IngestionRunRepository()
        return self._run_repository

    def record_stage(self, stage_id: str, status: str, *, progress: dict | None = None,
                     skip_reason: str | None = None, error: dict | None = None):
        """Persist a named business stage transition without failing the worker on audit outages."""
        run_id = self.state.get("run_id")
        if not run_id:
            return
        try:
            self._repository().record_stage(
                run_id, stage_id, status, progress=progress,
                skip_reason=skip_reason, error=error,
            )
            if status.upper() == "RUNNING":
                self._active_persisted_stages.add(stage_id)
            else:
                self._active_persisted_stages.discard(stage_id)
                if self._active_persisted_stage == stage_id:
                    self._active_persisted_stage = None
        except Exception as exc:
            print(f"[AUDIT] Could not persist stage {stage_id} for run {run_id}: {exc}", flush=True)

    def _checkpoint_provenance(self):
        run_id = self.state.get("run_id")
        if not run_id:
            raise RuntimeError("Cannot checkpoint a run without its durable run ID")
        record = self._repository().get_resume_record(self.job_id)
        if not record:
            raise RuntimeError("Cannot checkpoint: durable ingestion run record is missing")
        from kbfabric_ingest.ingest.checkpoints import stable_digest
        manifest = record.get("source_manifest_json") or {}
        context = record.get("run_context_json") or {}
        return (record, stable_digest(manifest), stable_digest(context))

    def cancellation_requested(self) -> bool:
        """Check the durable cancel flag at a pipeline safe boundary."""
        run_id = self.state.get("run_id")
        return bool(run_id and self._repository().is_cancel_requested(run_id))

    def start_worker(self) -> bool:
        """Claim worker start atomically against cancellation of a queued run."""
        run_id = self.state.get("run_id")
        return True if not run_id else self._repository().start_worker(run_id)

    def begin_store_publishing(self) -> bool:
        """Atomically establish whether cancellation or publishing won the boundary."""
        run_id = self.state.get("run_id")
        return True if not run_id else self._repository().begin_store_publishing(run_id)

    def record_store_write_unit(
        self, store_name: str, unit_key: str, status: str, *,
        progress: dict | None = None, error: dict | None = None,
    ) -> None:
        """Write store reconciliation evidence to the authoritative run ledger."""
        run_id = self.state.get("run_id")
        if not run_id:
            return
        attempt = int((self.state.get("audit_context") or {}).get("execution_attempt", 1))
        self._repository().record_store_write_unit(
            run_id, store_name, unit_key, status, attempt=attempt,
            progress=progress, error=error,
        )

    def mark_cancelled(self) -> None:
        """Persist cooperative cancellation while retaining the last checkpoint."""
        self.state["status"] = "cancelled"
        self.state["phase"] = "Cancelled at a safe stage boundary"
        self._write()
        run_id = self.state.get("run_id")
        if run_id:
            self._persist_run_status("CANCELLED")


    def commit_blocks_checkpoint(
        self, stage_id: str, blocks: list, *, block_kind: str,
        extra_payload: dict | None = None,
    ) -> None:
        """Persist typed Pydantic block outputs before advancing to the next stage."""
        run_id = self.state.get("run_id")
        if not run_id:
            return
        from kbfabric_ingest.ingest.checkpoints import write_checkpoint_artifact
        record, source_digest, context_digest = self._checkpoint_provenance()
        attempt = int((self.state.get("audit_context") or {}).get("execution_attempt", 1))
        artifact = (
            self.progress_dir.parent / "checkpoints" / run_id
            / f"{stage_id}-attempt-{attempt}.json"
        )
        payload = {
            "block_kind": block_kind,
            "blocks": [json.loads(item.model_dump_json()) for item in blocks],
            **(extra_payload or {}),
        }
        artifact_digest = write_checkpoint_artifact(artifact, stage_id=stage_id, payload=payload)
        self._repository().commit_checkpoint(
            run_id, stage_id,
            attempt=attempt,
            unit_key="all_blocks",
            artifact_uri=str(artifact), artifact_sha256=artifact_digest,
            source_manifest_sha256=source_digest, context_sha256=context_digest,
            cursor={"block_count": len(blocks), "block_kind": block_kind},
        )

    def commit_payload_checkpoint(self, stage_id: str, unit_key: str, payload: dict) -> None:
        """Persist a non-block recovery artifact under the same immutable provenance."""
        run_id = self.state.get("run_id")
        if not run_id:
            return
        from kbfabric_ingest.ingest.checkpoints import write_checkpoint_artifact
        _, source_digest, context_digest = self._checkpoint_provenance()
        attempt = int((self.state.get("audit_context") or {}).get("execution_attempt", 1))
        artifact = self.progress_dir.parent / "checkpoints" / run_id / f"{stage_id}-attempt-{attempt}.json"
        artifact_digest = write_checkpoint_artifact(artifact, stage_id=stage_id, payload=payload)
        self._repository().commit_checkpoint(
            run_id, stage_id, attempt=attempt, unit_key=unit_key,
            artifact_uri=str(artifact), artifact_sha256=artifact_digest,
            source_manifest_sha256=source_digest, context_sha256=context_digest,
            cursor={"payload_kind": unit_key},
        )

    def restore_latest_blocks_checkpoint(self):
        """Restore the latest validated parse/enrichment boundary for this run."""
        run_id = self.state.get("run_id")
        if not run_id:
            return None
        record, source_digest, context_digest = self._checkpoint_provenance()
        checkpoint = self._repository().get_latest_checkpoint(run_id)
        if not checkpoint:
            return None
        if (checkpoint["source_manifest_sha256"] != source_digest
                or checkpoint["context_sha256"] != context_digest):
            raise ValueError("Checkpoint provenance differs from the immutable source or run context")
        artifact = Path(checkpoint["artifact_uri"]).resolve()
        root = (self.progress_dir.parent / "checkpoints" / run_id).resolve()
        if not artifact.is_relative_to(root):
            raise ValueError("Checkpoint artifact is outside the run checkpoint directory")
        from kbfabric_ingest.ingest.checkpoints import read_checkpoint_artifact
        stage_id, payload = read_checkpoint_artifact(artifact, checkpoint["artifact_sha256"])
        if stage_id != checkpoint["stage_id"]:
            raise ValueError("Checkpoint stage does not match its PostgreSQL record")
        if stage_id == "stores.write" and payload.get("payload_kind") == "report_inputs":
            return {"stage_id": stage_id, "report_inputs": payload}
        from kbfabric_ingest.ingest.models import CanonicalBlock, EnrichedBlock
        kind = payload.get("block_kind")
        if kind == "canonical":
            blocks = [CanonicalBlock.model_validate(item) for item in payload.get("blocks", [])]
            return {"stage_id": stage_id, "canonical_blocks": blocks, "enriched_blocks": []}
        if kind in {"enriched", "admitted"}:
            blocks = [EnrichedBlock.model_validate(item) for item in payload.get("blocks", [])]
            enriched_payload = payload.get("enriched_blocks")
            enriched_blocks = (
                [EnrichedBlock.model_validate(item) for item in enriched_payload]
                if isinstance(enriched_payload, list)
                else blocks
            )
            return {
                "stage_id": stage_id, "enriched_blocks": enriched_blocks,
                "admitted_blocks": blocks if kind == "admitted" else [],
                "canonical_blocks": [item.block for item in enriched_blocks],
                "admission_stats": payload.get("admission_stats", {}),
            }
        raise ValueError("Checkpoint artifact contains an unsupported block type")

    def _persist_phase(self, phase: str, progress_pct: int):
        run_id = self.state.get("run_id")
        if not run_id:
            return
        try:
            repository = self._repository()
            progress = {
                "progress_pct": max(0, min(100, int(progress_pct))),
                "files_done": int(self.state.get("files_done", 0) or 0),
                "files_total": int(self.state.get("files_total", 0) or 0),
                "blocks_extracted": int(self.state.get("blocks_extracted", 0) or 0),
                "entities_found": int(self.state.get("entities_found", 0) or 0),
                "relationships_found": int(self.state.get("relationships_found", 0) or 0),
                "weaviate_chunks": int(self.state.get("weaviate_chunks", 0) or 0),
                "neo4j_nodes": int(self.state.get("neo4j_nodes", 0) or 0),
            }
            repository.record_progress(run_id, phase, progress)
            stage_by_phase = {
                "Embedding Model Preflight": "dependencies.ready",
                "Parsing Documents": "documents.parsed",
                "Storing in Weaviate & Neo4j": "stores.write",
                "Generating Reports": "run.verify_report",
            }
            if phase.startswith("PII Masking"):
                stage_by_phase[phase] = "privacy.applied"
            elif phase.startswith("LLM IE (Text two-pass)"):
                stage_by_phase[phase] = "lane2.pass1"
                # Pass 1 and Pass 2 are executed together per block by Lane 2;
                # keep both business stages active for the duration of that work.
                if "lane2.pass2" not in self._active_persisted_stages:
                    self.record_stage("lane2.pass2", "RUNNING", progress=progress)
            elif phase.startswith("LLM IE (Text)"):
                stage_by_phase[phase] = "lane2.pass2"
                if "lane2.pass1" not in self._active_persisted_stages:
                    self.record_stage(
                        "lane2.pass1", "SKIPPED",
                        skip_reason="Configured text extraction uses the single-pass route.",
                    )
            next_stage = stage_by_phase.get(phase)
            if self._active_persisted_stage and self._active_persisted_stage != next_stage:
                self.record_stage(self._active_persisted_stage, "SUCCEEDED", progress=progress)
                self._active_persisted_stage = None
            if next_stage and next_stage != self._active_persisted_stage:
                self.record_stage(next_stage, "RUNNING", progress=progress)
                self._active_persisted_stage = next_stage
        except Exception as exc:
            print(f"[AUDIT] Could not persist progress for run {run_id}: {exc}", flush=True)
    
    def set_files(self, done: int, total: int, current: str = ""):
        """Update file progress."""
        self.state["files_done"] = done
        self.state["files_total"] = total
        self.state["current_file"] = current
        self._write()
    
    def set_counts(self, blocks: int = None, entities: int = None,
                   relationships: int = None, images: int = None,
                   weaviate: int = None, neo4j: int = None):
        """Update extraction counts."""
        if blocks is not None:
            self.state["blocks_extracted"] = blocks
        if entities is not None:
            self.state["entities_found"] = entities
        if relationships is not None:
            self.state["relationships_found"] = relationships
        if images is not None:
            self.state["images_processed"] = images
        if weaviate is not None:
            self.state["weaviate_chunks"] = weaviate
        if neo4j is not None:
            self.state["neo4j_nodes"] = neo4j
        run_id = self.state.get("run_id")
        if run_id:
            try:
                self._repository().record_progress(
                    run_id,
                    self.state.get("phase", "unknown"),
                    {
                        key: int(self.state.get(key, 0) or 0)
                        for key in (
                            "files_done", "files_total", "blocks_extracted",
                            "entities_found", "relationships_found", "weaviate_chunks",
                            "neo4j_nodes", "warning_count",
                        )
                    },
                )
            except Exception as exc:
                print(f"[AUDIT] Could not persist aggregate counts for run {run_id}: {exc}", flush=True)
        self._write()
    
    def _persist_run_status(self, status: str, error_message: str | None = None,
                            error_details: dict | None = None):
        run_id = self.state.get("run_id")
        if not run_id:
            return
        try:
            self._repository().update_status(
                run_id, status, error_message=error_message, error_details=error_details
            )
        except Exception as exc:
            print(f"[AUDIT] Could not update PostgreSQL run {run_id}: {exc}", flush=True)

    def complete(self, report_paths: dict = None, warning_count: int = 0):
        """Mark job as completed."""
        self.state["status"] = "completed"
        self.state["phase"] = "Completed"
        self.state["progress_pct"] = 100
        if report_paths:
            self.state["report_paths"] = report_paths
        try:
            self.state["warning_count"] = int(warning_count or 0)
        except Exception:
            self.state["warning_count"] = 0
        self._write()
        run_id = self.state.get("run_id")
        if run_id and self._active_persisted_stage:
            completed_stage = self._active_persisted_stage
            try:
                self._repository().record_stage(
                    run_id, completed_stage, "SUCCEEDED",
                    progress={"progress_pct": 100},
                )
            except Exception as exc:
                print(f"[AUDIT] Could not complete stage for run {run_id}: {exc}", flush=True)
            self._active_persisted_stage = None
            self._active_persisted_stages.discard(completed_stage)
        if run_id:
            for stage_id in list(self._active_persisted_stages):
                self.record_stage(stage_id, "SUCCEEDED", progress={"progress_pct": 100})
            try:
                self._repository().skip_pending_stages(
                    run_id, "No work was required for this stage on the completed run path."
                )
            except Exception as exc:
                print(f"[AUDIT] Could not close unused stages for run {run_id}: {exc}", flush=True)
        self._persist_run_status("COMPLETED")
        print(f"[COMPLETE] Job {self.job_id} finished successfully", flush=True)
    
    def fail(self, error: str):
        """Mark job as failed."""
        self.state["status"] = "failed"
        self.state["phase"] = "Failed"
        self.state["error"] = error
        self._write()
        run_id = self.state.get("run_id")
        if run_id and self._active_persisted_stage:
            failed_stage = self._active_persisted_stage
            try:
                self._repository().record_stage(
                    run_id, failed_stage, "FAILED",
                    error={"message": str(error)[:500]},
                )
            except Exception as exc:
                print(f"[AUDIT] Could not fail stage for run {run_id}: {exc}", flush=True)
            self._active_persisted_stage = None
            self._active_persisted_stages.discard(failed_stage)
        if run_id:
            for stage_id in list(self._active_persisted_stages):
                self.record_stage(stage_id, "FAILED", error={"message": str(error)[:500]})
        from kbfabric_ingest.ingest.retry_policy import classify_failure
        failure = classify_failure(error)
        self._persist_run_status("FAILED", error_message=error, error_details=failure)
        print(f"[FAILED] Job {self.job_id}: {error}", flush=True)


def run_ingestion(job_id: str, domain: str, input_dir: str, force_rewrite: bool, hitl_mode: str | None = None, product_line_id: str | None = None, document_nature: str = "qe_assets", run_id: str | None = None, submitted_by: str | None = None, auth_source: str | None = None, customer_id: str | None = None, customer_ontology_version: int | None = None):
    """Execute the ingestion pipeline."""
    
    persist_root = _resolve_persist_dir(input_dir)
    progress_dir = persist_root / "progress"
    reports_dir = persist_root / "reports"
    logs_dir = persist_root / "logs" / "ingestion_jobs"
    progress_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    report_base = _job_report_base(job_id)
    job_console_log = logs_dir / f"{report_base}.log"
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    with open(job_console_log, "a", encoding="utf-8", buffering=1) as log_handle:
        sys.stdout = _TeeStream(original_stdout, log_handle)
        sys.stderr = _TeeStream(original_stderr, log_handle)

        try:
            audit_context = json.loads(os.environ.get("KBFABRIC_INGESTION_AUDIT_CONTEXT", "{}"))
            if not isinstance(audit_context, dict):
                raise ValueError("audit context must be a JSON object")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            audit_context = {}
            print(f"[AUDIT][WARNING] Could not load frozen run context: {exc}", flush=True)

        tracker = JobProgressTracker(
            job_id, domain, input_dir, progress_dir=progress_dir,
            product_line_id=product_line_id, document_nature=document_nature,
            run_id=run_id, submitted_by=submitted_by, auth_source=auth_source,
            customer_id=customer_id,
            customer_ontology_version=customer_ontology_version,
            audit_context=audit_context,
        )
        # A queued run may receive cancellation before Ray starts this process.
        # Honor the durable flag before changing status to RUNNING or doing work.
        if not tracker.start_worker():
            tracker.mark_cancelled()
            _job_event("ingestion.job.end", status="cancelled", cancel_boundary="before_worker_start")
            return 0
        service_started = False

        # Structured, server-visible job logging (flows through stdout into the
        # tee'd job log). Import here so sys.path is already configured above.
        try:
            from kbfabric_ingest.utils.ingestion_observability import (
                get_ingestion_logger,
                log_stage_start,
                log_stage_end,
                log_stage_error,
            )

            obs_logger = get_ingestion_logger("kbfabric_ingest.job")
        except Exception:
            obs_logger = None

        def _job_event(event: str, **fields):
            if obs_logger is None:
                return
            try:
                obs_logger.info(event, job_id=job_id, domain=domain, **fields)
            except Exception:
                pass

        _job_event(
            "ingestion.job.start",
            input_dir=input_dir,
            product_line_id=product_line_id,
            document_nature=document_nature,
            hitl_mode=hitl_mode,
            customer_id=customer_id,
            force_rewrite=bool(force_rewrite),
            run_id=run_id,
            submitted_by=submitted_by,
            auth_source=auth_source,
            audit_context=audit_context,
            persist_dir=str(persist_root),
            job_log_path=str(job_console_log),
        )

        def write_fallback_report(error_message: str):
            try:
                from kbfabric_ingest.reports.unified_report import UnifiedIngestionReportGenerator

                run_summary = {
                    "job_id": job_id,
                    "domain": domain,
                    "input_dir": input_dir,
                    "status": "error",
                    "error": error_message,
                    "errors": [{"step": "preflight", "error": error_message}],
                }
                placeholder = "<div class='container'><h2>Report Unavailable</h2><p>No report content was generated for this run.</p></div>"
                UnifiedIngestionReportGenerator().generate(
                    domain=domain,
                    timestamp=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
                    job_id=job_id,
                    run_summary=run_summary,
                    document_metrics=[],
                    semantic_html=placeholder,
                    audit_html=placeholder,
                    kg_html=placeholder,
                    json_report_path=None,
                    log_file_path=str(job_console_log),
                    output_dir=reports_dir,
                )
            except Exception:
                # Never block the job on report generation
                pass
    
        try:
            if force_rewrite:
                from kbfabric_ingest.ingest.config import get_app_config as get_ingestion_config
                if not get_ingestion_config().get("ingestion", {}).get("force_rewrite_enabled", False):
                    raise PermissionError(
                        "Force rewrite is disabled by KBFABRIC_FORCE_REWRITE_ENABLED."
                    )
                if not customer_id or not product_line_id or not customer_ontology_version:
                    raise ValueError(
                        "Force rewrite requires explicit customer_id, product_line_id, "
                        "and frozen customer_ontology_version."
                    )

            # Phase 1: Initialize
            tracker.set_phase("Initializing", 1, 5)
            print(f"\n{'='*60}", flush=True)
            print(f"  Pulse Knowledge Fabric - Ingestion Job", flush=True)
            print(f"{'='*60}", flush=True)
            print(f"  Job ID: {job_id}", flush=True)
            print(f"  Domain: {domain}", flush=True)
            print(f"  Input: {input_dir}", flush=True)
            print(f"  Force Rewrite: {force_rewrite}", flush=True)
            print(f"{'='*60}\n", flush=True)
            
            # Initialize OpenTelemetry for Ray job worker
            try:
                from kbfabric_ingest._common.telemetry import setup_telemetry
                setup_telemetry()
                print("[TELEMETRY] OpenTelemetry initialized for Ray job worker", flush=True)
            except Exception as e:
                print(f"[TELEMETRY][WARNING] Failed to initialize telemetry: {e}", flush=True)
                # Don't fail the job if telemetry fails
            
            # Enterprise fail-fast: embedding model must be available BEFORE expensive IE
            tracker.set_phase("Embedding Model Preflight", 1, 8)
            preflight_started = None
            if obs_logger is not None:
                preflight_started = log_stage_start(obs_logger, "preflight", job_id=job_id, domain=domain)
            try:
                from kbfabric_ingest._common.config import get_app_config
                from custom_ml.embeddings import EmbeddingsClient

                cfg = get_app_config()
                emb = EmbeddingsClient(cfg)
                embeddings_cfg = cfg.get("embeddings", {})
                model_name = embeddings_cfg.get("model_name", "unknown")
                print(f"[PREFLIGHT] Loading embedding model '{model_name}' (enterprise offline, local-path only)...", flush=True)
                emb.load_model()
                stats = emb.get_load_stats()
                print(
                    f"[PREFLIGHT] Embedding model ready. model_name={model_name}, "
                    f"model_path={stats.get('model_path')}, device={stats.get('device')}",
                    flush=True
                )
                if obs_logger is not None:
                    log_stage_end(
                        obs_logger,
                        "preflight",
                        preflight_started,
                        job_id=job_id,
                        domain=domain,
                        model_name=model_name,
                        model_path=stats.get("model_path"),
                        device=stats.get("device"),
                    )
            except Exception as e:
                # Fail immediately (no wasted IE time, no 600s hang)
                err = f"Embedding model preflight failed: {e}"
                print(f"[PREFLIGHT][ERROR] {err}", flush=True)
                if obs_logger is not None:
                    log_stage_error(obs_logger, "preflight", e, job_id=job_id, domain=domain, started_at=preflight_started)
                tracker.fail(err)
                write_fallback_report(err)
                _job_event("ingestion.job.end", status="failed", error=err)
                return 1

            # Import here to avoid slow startup
            from kbfabric_ingest.ingest.ingestion_service import IngestionService
            
            tracker.set_phase("Loading Service", 1, 10)
            report_only_resume = audit_context.get("resume_from_stage") == "run.verify_report"
            if report_only_resume:
                # Report recovery must not run constructor-time schema/index DDL.
                service = IngestionService.__new__(IngestionService)
                from kbfabric_ingest.reports.unified_report import UnifiedIngestionReportGenerator
                service.unified_report_generator = UnifiedIngestionReportGenerator()
            else:
                service = IngestionService()
            service_started = True
            
            # A rewrite is a one-time pre-run action. Resuming from a checkpoint must
            # never clear the product line a second time.
            execution_attempt = int(audit_context.get("execution_attempt", 1) or 1)
            if _should_apply_force_rewrite(force_rewrite, execution_attempt):
                tenant_context = service.customer_context_resolver.resolve(
                    customer_id=customer_id
                )
                frozen_ontology = service.ontology_context_resolver.resolve(
                    customer_id=tenant_context.customer_id,
                    domain_id=domain,
                    product_line_id=product_line_id,
                    customer_ontology_version=customer_ontology_version,
                )
                if frozen_ontology.product_line_id != product_line_id:
                    raise ValueError("Resolved ontology does not match the requested product line")

                tracker.set_phase("Rebuilding Product-Line Scope", 2, 15)
                print(
                    f"[CLEAR] Rebuilding customer={customer_id}, domain={domain}, "
                    f"product_line={product_line_id}...",
                    flush=True,
                )
                clear_started = None
                if obs_logger is not None:
                    clear_started = log_stage_start(
                        obs_logger,
                        "clear_product_line",
                        job_id=job_id,
                        customer_id=customer_id,
                        domain=domain,
                        product_line_id=product_line_id,
                    )
                cleanup_result = service.clear_product_line(
                    customer_id=customer_id,
                    tenant_id=tenant_context.tenant_id,
                    domain_id=domain,
                    product_line_id=product_line_id,
                )
                print(f"[CLEAR] Product-line cleanup: {cleanup_result}", flush=True)
                if obs_logger is not None:
                    log_stage_end(
                        obs_logger,
                        "clear_product_line",
                        clear_started,
                        job_id=job_id,
                        **cleanup_result,
                    )
            elif force_rewrite:
                print(
                    f"[CLEAR] Skipping force rewrite on resume attempt {execution_attempt}; "
                    "the original scope cleanup already ran before this run's first attempt.",
                    flush=True,
                )
            
            # Phase 3: Run ingestion with progress tracking
            tracker.set_phase("Ingesting Documents", 3, 20)
            tracker.update(status="running")
            
            input_path = Path(input_dir)
            if not input_path.exists():
                raise FileNotFoundError(f"Input directory not found: {input_dir}")
            
            # Count files
            # Enterprise policy: do not hard-filter file types here (parser decides what it can read).
            files = [f for f in input_path.glob("*") if f.is_file()]
            tracker.set_files(0, len(files))
            
            # Change to a real filesystem directory so reports are saved there (not in Ray temp dir)
            original_cwd = os.getcwd()
            os.chdir(str(persist_root))
            print(f"[JOB] Working directory for reports: {persist_root}", flush=True)
            
            # Run the ingestion
            try:
                if report_only_resume:
                    tracker.set_phase("Generating Reports", 7, 95)
                    restored = tracker.restore_latest_blocks_checkpoint()
                    if not restored or not restored.get("report_inputs"):
                        raise ValueError("Report recovery requires a verified report-input checkpoint")
                    result = service.resume_report_from_checkpoint(restored["report_inputs"])
                else:
                    result = service.run_ingestion(
                        input_path,
                        domain,
                        progress_tracker=tracker,
                        job_id=job_id,
                        job_output_log_path=str(job_console_log),
                        hitl_mode=hitl_mode,
                        product_line_id=product_line_id,
                        document_nature=document_nature,
                        customer_id=customer_id,
                        customer_ontology_version=customer_ontology_version,
                        project_ids=_metadata_values_as_raw(
                            (audit_context.get("submission_metadata") or {}).get("project_ids")
                        ),
                        context_tags=_metadata_values_as_raw(
                            (audit_context.get("submission_metadata") or {}).get("context_tags")
                        ),
                    )
            except Exception as exc:
                from kbfabric_ingest.ingest.ingestion_service import IngestionCancelRequested
                if not isinstance(exc, IngestionCancelRequested):
                    raise
                tracker.mark_cancelled()
                _job_event("ingestion.job.end", status="cancelled", cancel_boundary="last_completed_stage")
                return 0
            finally:
                os.chdir(original_cwd)  # Restore original directory
            
            # Phase 8: Complete
            if result.get("error"):
                tracker.fail(result["error"])
                _job_event(
                    "ingestion.job.end",
                    status="failed",
                    error=str(result.get("error")),
                    total_files=result.get("total_files", 0),
                    total_blocks=result.get("total_blocks", 0),
                    duration_sec=tracker.state.get("elapsed_sec"),
                )
                return 1
            
            # This marker describes the whole fabric, so one product-line rewrite
            # must never claim that every scope has been migrated to a new schema.
            if force_rewrite:
                print(
                    "[JOB] Fabric-wide schema fingerprint unchanged by product-line rewrite",
                    flush=True,
                )
            else:
                try:
                    from kbfabric_ingest.scheduler.schema_fingerprint import compute_fingerprint, write_fingerprint
                    fp = compute_fingerprint()
                    write_fingerprint(fp)
                    print(f"[JOB] Schema fingerprint updated", flush=True)
                except Exception as fp_err:
                    print(f"[JOB] WARNING: Failed to update schema fingerprint: {fp_err}", flush=True)
            
            # Update final counts
            tracker.set_counts(
                blocks=result.get("total_blocks", 0),
                entities=result.get("entities_extracted", 0),
                relationships=result.get("relationships_extracted", 0),
                weaviate=result.get("weaviate_chunks", 0),
                neo4j=result.get("neo4j_nodes", 0)
            )
            
            # Complete (non-fatal errors are warnings)
            warning_count = 0
            try:
                warning_count = len(result.get("errors") or [])
            except Exception:
                warning_count = 0
            tracker.complete(report_paths=result.get("report_paths", {}), warning_count=warning_count)

            _job_event(
                "ingestion.job.end",
                status=result.get("status", "success"),
                total_files=result.get("total_files", 0),
                total_blocks=result.get("total_blocks", 0),
                entities_extracted=result.get("entities_extracted", 0),
                relationships_extracted=result.get("relationships_extracted", 0),
                weaviate_chunks=result.get("weaviate_chunks", 0),
                neo4j_nodes=result.get("neo4j_nodes", 0),
                neo4j_relationships=result.get("neo4j_relationships", 0),
                report_paths=result.get("report_paths", {}),
                warning_count=warning_count,
                duration_sec=tracker.state.get("elapsed_sec"),
            )

            # Print summary
            print(f"\n{'='*60}", flush=True)
            print(f"  INGESTION COMPLETE", flush=True)
            print(f"{'='*60}", flush=True)
            print(f"  Duration: {tracker.state['elapsed_sec']:.1f}s", flush=True)
            print(f"  Files: {result.get('total_files', 0)}", flush=True)
            print(f"  Blocks: {result.get('total_blocks', 0)}", flush=True)
            print(f"  Entities: {result.get('entities_extracted', 0)}", flush=True)
            
            # Image breakdown (normal vs vector diagrams)
            total_images = result.get('total_images', 0)
            vector_diagrams = result.get('vector_diagrams', 0)
            normal_images = total_images - vector_diagrams
            if total_images > 0:
                print(f"  Images: {total_images} (Normal: {normal_images}, Vector Diagrams: {vector_diagrams})", flush=True)
            
            print(f"  Weaviate chunks: {result.get('weaviate_chunks', 0)}", flush=True)
            print(f"  Neo4j nodes: {result.get('neo4j_nodes', 0)}", flush=True)
            print(f"  Neo4j relationships: {result.get('neo4j_relationships', 0)}", flush=True)
            print(f"{'='*60}\n", flush=True)
            
            return 0
            
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            print(f"\n[ERROR] {error_msg}", flush=True)
            print(traceback.format_exc(), flush=True)
            tracker.fail(error_msg)
            _job_event("ingestion.job.error", status="failed", error=error_msg)
            if not service_started:
                write_fallback_report(error_msg)
            return 1
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr


def main():
    parser = argparse.ArgumentParser(description="Run document ingestion job")
    parser.add_argument("--job-id", required=True, help="Unique job identifier")
    parser.add_argument("--run-id", default=None, help="PostgreSQL ingestion run audit ID")
    parser.add_argument("--submitted-by", default=None, help="Authenticated actor ID resolved by the API")
    parser.add_argument("--auth-source", choices=("kong_jwt", "dev_test_token"), default=None, help="Trusted identity source recorded for audit")
    parser.add_argument("--customer-id", required=True, help="Explicit active customer workspace ID frozen by the API")
    parser.add_argument("--customer-ontology-version", required=True, type=int, help="Customer ontology snapshot version frozen when the API accepted this run")
    parser.add_argument("--domain", required=True, help="Domain name (e.g., wealth_portfolio)")
    parser.add_argument("--input-dir", required=True, help="Path to input documents")
    parser.add_argument("--product-line-id", required=True, help="Explicit configured customer product-line ID")
    parser.add_argument("--document-nature", choices=("qe_assets", "technology_artifacts"), required=True, help="Ingestion Studio lane classification")
    parser.add_argument("--force-rewrite", action="store_true", help="Clear existing data first")
    parser.add_argument(
        "--hitl-mode",
        choices=("auto_write", "review_ambiguous", "review_all"),
        default=None,
        help="Review policy selected for this run (defaults to service config)",
    )
    
    args = parser.parse_args()
    
    exit_code = run_ingestion(
        job_id=args.job_id,
        domain=args.domain,
        input_dir=args.input_dir,
        force_rewrite=args.force_rewrite,
        hitl_mode=args.hitl_mode,
        product_line_id=args.product_line_id,
        document_nature=args.document_nature,
        run_id=args.run_id,
        submitted_by=args.submitted_by,
        auth_source=args.auth_source,
        customer_id=args.customer_id,
        customer_ontology_version=args.customer_ontology_version,
    )
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

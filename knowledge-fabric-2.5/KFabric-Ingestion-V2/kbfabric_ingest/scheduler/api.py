"""
Pulse Knowledge Fabric - Ingestion API

Schedules and monitors document ingestion jobs via Ray Jobs.
Supports parallel execution with progress tracking.

Endpoints:
    POST /jobs          - Submit new ingestion job (directory-based)
    POST /jobs/upload   - Submit new ingestion job with file uploads
    GET  /jobs          - List all jobs
    GET  /jobs/{id}     - Get job status and progress
    GET  /jobs/{id}/logs - Get job logs
    DELETE /jobs/{id}   - Stop a job
    GET  /progress/{id} - Get job-specific progress file
    GET  /domains       - List available domains
    GET  /reports                 - List available reports
    GET  /reports/get-report/{id} - Get unified report by job ID
    GET  /health        - Basic health check
    GET  /health/live   - Kubernetes liveness probe
    GET  /health/ready  - Kubernetes readiness probe

Run with:
    uvicorn kbfabric_ingest.scheduler.api:app --host 0.0.0.0 --port 6002
"""

import asyncio
import hashlib
import json
import os
import shutil
import shlex
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Literal, Optional

import ray
from ray.job_submission import JobSubmissionClient, JobStatus
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from kbfabric_ingest.security.auth import AuthenticatedActor, require_authenticated_actor
from kbfabric_ingest.ingest.repository.ingestion_run_repository import IngestionRunRepository
from kbfabric_ingest.ingest.repository.hitl_review_repository import HITLReviewRepository
from kbfabric_ingest.reports.unified_report import UnifiedIngestionReportGenerator

from kbfabric_ingest._common.telemetry import (
    setup_telemetry, instrument_fastapi, get_metrics_response, record_ray_job_submitted
)
from kbfabric_ingest._common.config import BASE_DIR, get_app_config, get_domains_full
from kbfabric_ingest._common.env_config import load_dotenv_file, require_env
from kbfabric_ingest._common.logger import get_logger
from kbfabric_ingest.health_checks import check_weaviate, check_neo4j
from kbfabric_ingest.scheduler.schema_fingerprint import (
    compute_fingerprint,
    get_stored_fingerprint,
)
from kbfabric_ingest.security.api import security_router
from kbfabric_ingest.ingest.hitl_api import hitl_router
from kbfabric_ingest.fabric_foundry.api import fabric_foundry_router
from kbfabric_ingest.fabric_foundry.bootstrap.service import FabricFoundryBootstrap
from custom_ml.embeddings import EmbeddingsClient

# Directories
PROGRESS_DIR = BASE_DIR / "progress"
REPORTS_DIR = BASE_DIR / "reports"
UPLOADS_DIR = BASE_DIR / "uploads" / "temp"
PROGRESS_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Ray Jobs client — URL from env (no localhost default).
# RAY_DASHBOARD_URL: in-container client (usually http://127.0.0.1:8266)
# RAY_DASHBOARD_PUBLIC_URL: browser links on the LAN host (.17 / .18)
load_dotenv_file()
_job_client: Optional[JobSubmissionClient] = None
RAY_DASHBOARD_URL = require_env("RAY_DASHBOARD_URL")
RAY_DASHBOARD_PUBLIC_URL = (os.environ.get("RAY_DASHBOARD_PUBLIC_URL") or "").strip() or RAY_DASHBOARD_URL

# Startup time for uptime tracking (Kubernetes liveness)
_startup_time: float = time.time()

# Logger
logger = get_logger(__name__)

# Global registry for cleanup tasks
_cleanup_tasks: dict[str, asyncio.Task] = {}


VENV_RAY_SETUP_REMINDER = (
    "Create .venv_ray with: python3.11 -m venv .venv_ray && "
    ".venv_ray/bin/pip install --no-index --find-links ./pip_deps -r ray_job_requirements.txt "
    "Or run: ./scripts/setup_venv_ray.sh"
)


def _ensure_venv_ray_ready() -> None:
    """
    Ensure a Python venv exists for Ray jobs. Prefer .venv_ray, fallback to .venv.
    Raises HTTPException 503 if neither is usable.
    """
    for venv_name in (".venv_ray", ".venv"):
        venv_dir = BASE_DIR / venv_name
        python_bin = venv_dir / "bin" / "python"
        if venv_dir.exists() and venv_dir.is_dir() and python_bin.exists() and python_bin.is_file():
            return
    raise HTTPException(
        status_code=503,
        detail="Neither .venv_ray nor .venv found. Create .venv or run setup_venv_ray.sh."
    )


def _resolve_ray_worker_dir(persist_dir: str) -> str:
    """Path as seen by the Ray worker. Defaults to persist_dir if RAY_WORKER_DIR is unset."""
    return (os.environ.get("RAY_WORKER_DIR") or "").strip() or persist_dir


def _get_ray_python_path(persist_dir: str) -> str:
    """Full path to Python for Ray job entrypoint. Checks host, returns worker-visible path.

    Priority:
      1. RAY_PYTHON_PATH env var (explicit override, e.g. for containerised Ray)
      2. .venv_ray or .venv on the host (local / host-based Ray)
      3. Plain 'python' (relies on Ray container PATH, e.g. anaconda)
    """
    explicit = os.environ.get("RAY_PYTHON_PATH", "").strip()
    if explicit:
        return explicit

    ray_dir = _resolve_ray_worker_dir(persist_dir)
    host_base = Path(persist_dir.rstrip("/"))
    for venv_name in (".venv_ray", ".venv"):
        if (host_base / venv_name / "bin" / "python").exists():
            return f"{ray_dir.rstrip('/')}/{venv_name}/bin/python"
    return "python"

def _get_ray_runtime_env(
    persist_dir: str,
    config: dict,
    excludes_extra: Optional[list[str]] = None,
    *,
    audit_context: Optional[dict[str, Any]] = None,
) -> dict:
    """Build runtime_env for Ray job submission. No pip - uses .venv_ray.

    Omits working_dir when persist_dir is a volume-mounted path (Docker deployment).
    The Ray worker runs in a container with -v persist_dir:persist_dir, so the job
    uses the mount directly. This avoids the ~100MB package upload limit that causes
    HTTPRequestEntityTooLarge when working_dir is used.
    """
    # Service URLs for Ray worker — always from env-built config (no localhost defaults)
    weaviate_url = config["weaviate"]["url"]
    neo4j_uri = config["neo4j"]["uri"]
    neo4j_password = config["neo4j"]["password"]
    ollama_url = config["ollama"]["base_url"]
    otel_endpoint = require_env("OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_enabled = require_env("OTEL_ENABLED")

    ray_dir = _resolve_ray_worker_dir(persist_dir)
    nltk_data_path = f"{ray_dir.rstrip('/')}/nltk_data"
    embeddings_model_dir = (
        (os.environ.get("KBFABRIC_EMBEDDINGS_MODEL_DIR") or "").strip()
        or config["embeddings"]["local_model_path"]
    )
    if os.environ.get("RAY_WORKER_DIR") and embeddings_model_dir.startswith(persist_dir):
        embeddings_model_dir = embeddings_model_dir.replace(persist_dir.rstrip("/"), ray_dir.rstrip("/"), 1)
    env_vars: dict[str, str] = {
        "PYTHONPATH": ray_dir.rstrip("/"),
        "NLTK_DATA": nltk_data_path,
        "KBFABRIC_EMBEDDINGS_MODEL_DIR": embeddings_model_dir,
        "KBFABRIC_INGEST_PERSIST_DIR": ray_dir,
        "WEAVIATE_URL": weaviate_url,
        "NEO4J_URI": neo4j_uri,
        "OLLAMA_URL": ollama_url,
        "OTEL_EXPORTER_OTLP_ENDPOINT": otel_endpoint,
        "OTEL_ENABLED": otel_enabled,
    }
    # The ingestion worker initializes its own repositories and model clients.
    # Forward only the backend and model-router settings it needs; inheriting the
    # entire API process environment would also copy unrelated credentials.
    worker_setting_names = (
        "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER",
        "POSTGRES_PASSWORD", "POSTGRES_POOL_SIZE", "NEO4J_USER",
        "WEAVIATE_API_KEY", "MODEL_ROUTER_ENABLED", "MODEL_ROUTER_CHAT_URL",
        "MODEL_ROUTER_GENERATE_URL", "MODEL_ROUTER_AUTH_TOKEN",
        "MODEL_ROUTER_TEXT_MODEL", "MODEL_ROUTER_MAX_TOKENS",
        "MODEL_ROUTER_MAX_RETRIES", "PII_ENGINE", "PII_MASK_POLICY",
        "PII_QUARANTINE_THRESHOLD", "PII_USE_FAKER", "PII_FAKER_SEED",
        "PII_ENTITIES", "INGESTION_HITL_MODE", "INGESTION_TEXT_IE_MODE",
    )
    for name in worker_setting_names:
        value = os.environ.get(name)
        if value is not None:
            env_vars[name] = value
    if neo4j_password:
        env_vars["NEO4J_PASSWORD"] = neo4j_password
    if audit_context is not None:
        env_vars["KBFABRIC_INGESTION_AUDIT_CONTEXT"] = json.dumps(audit_context)

    # No working_dir: job runs from volume mount (avoids HTTPRequestEntityTooLarge)
    return {
        "config": {"setup_timeout_seconds": 3600},
        "env_vars": env_vars,
    }


def create_job_upload_dir(job_id: str) -> Path:
    """Create unique directory for job uploads."""
    job_dir = UPLOADS_DIR / f"job_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def cleanup_upload_dir(upload_dir: Path):
    """Safely remove upload directory."""
    try:
        if upload_dir.exists():
            shutil.rmtree(upload_dir)
            logger.info(f"Cleaned up upload directory: {upload_dir}")
    except Exception as e:
        logger.error(f"Failed to cleanup {upload_dir}: {e}")


def schedule_cleanup(job_id: str, upload_dir: Path):
    """
    Schedule asynchronous cleanup of upload directory after job completion.

    Strategy:
    - Wait up to 72 hours for job completion
    - If job still running, schedule age-based cleanup (168 hours max)
    - Always cleanup eventually to prevent disk space issues
    """
    async def cleanup_task():
        try:
            # Wait for job to complete (up to 72 hours)
            await wait_for_job_completion(job_id, timeout_hours=72)

            # Failed/cancelled upload runs retain source files for a bounded resume window.
            run_record = await asyncio.to_thread(
                IngestionRunRepository().get_resume_record, job_id
            )
            if run_record and run_record.get("status") in {"FAILED", "CANCELLED"}:
                retention_hours = max(
                    1, int(os.environ.get("KBFABRIC_RESUME_SOURCE_RETENTION_HOURS", "72"))
                )
                logger.info(
                    f"Retaining resume sources for {job_id} for {retention_hours} hours"
                )
                schedule_age_based_cleanup(job_id, upload_dir, retention_hours)
                return

            # Job completed - cleanup immediately
            cleanup_upload_dir(upload_dir)

        except asyncio.TimeoutError:
            # Job still running after 72 hours - schedule age-based cleanup
            logger.warning(f"Job {job_id} still running after 72 hours - scheduling age-based cleanup")
            schedule_age_based_cleanup(job_id, upload_dir, max_age_hours=168)  # 1 week

        except Exception as e:
            logger.error(f"Cleanup failed for job {job_id}: {e}")
        finally:
            # Remove from active tasks
            _cleanup_tasks.pop(job_id, None)

    # Start background cleanup task
    task = asyncio.create_task(cleanup_task())
    _cleanup_tasks[job_id] = task


async def wait_for_job_completion(job_id: str, timeout_hours: int = 72):
    """
    Poll Ray job status until completion or timeout.

    Raises TimeoutError if job doesn't complete within timeout.
    """
    client = get_job_client()
    start_time = time.time()
    timeout_seconds = timeout_hours * 3600

    while time.time() - start_time < timeout_seconds:
        try:
            status = client.get_job_status(job_id)
            if status in [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.STOPPED]:
                return  # Job finished

            await asyncio.sleep(30)  # Check every 30 seconds

        except Exception as e:
            logger.warning(f"Error checking job {job_id} status: {e}")
            await asyncio.sleep(60)  # Wait longer on errors

    raise asyncio.TimeoutError(f"Job {job_id} did not complete within {timeout_hours} hours")


def schedule_age_based_cleanup(job_id: str, upload_dir: Path, max_age_hours: int):
    """
    Fallback cleanup based on directory age, not job status.
    Used when jobs run longer than expected.
    """
    async def age_cleanup_task():
        # Wait for the maximum age
        await asyncio.sleep(max_age_hours * 3600)

        try:
            if not upload_dir.exists():
                return  # Already cleaned up

            # Check if job is still running before cleanup
            try:
                client = get_job_client()
                status = client.get_job_status(job_id)
                if status in [JobStatus.RUNNING, JobStatus.PENDING]:
                    # Job still active - schedule another check in 24 hours
                    logger.warning(f"Job {job_id} still active after {max_age_hours}h - extending cleanup")
                    schedule_age_based_cleanup(job_id, upload_dir, 24)
                    return
            except Exception:
                # If we can't check status, assume job is done or failed
                pass

            try:
                run_record = IngestionRunRepository().get_resume_record(job_id)
                if run_record and run_record.get("status") in {
                    "QUEUED", "VALIDATING", "RUNNING", "RETRY_WAIT", "CANCEL_REQUESTED"
                }:
                    logger.warning(f"Run {job_id} is active; extending source retention")
                    schedule_age_based_cleanup(job_id, upload_dir, 24)
                    return
            except Exception as exc:
                logger.warning(f"Could not check durable run state before cleanup: {exc}")

            # Safe to cleanup
            cleanup_upload_dir(upload_dir)
            logger.info(f"Age-based cleanup completed for job {job_id}")

        except Exception as e:
            logger.error(f"Age-based cleanup failed for job {job_id}: {e}")
        finally:
            # Remove from active tasks
            _cleanup_tasks.pop(f"age_{job_id}", None)

    # Start age-based cleanup task
    task = asyncio.create_task(age_cleanup_task())
    _cleanup_tasks[f"age_{job_id}"] = task


# ============================================================
# Pydantic Models
# ============================================================

class IngestionReviewPolicyOption(BaseModel):
    """One policy choice shown before the user starts an ingestion run."""

    value: Literal["review_all", "review_ambiguous", "auto_write"]
    label: str
    description: str


class IngestionReviewPolicyResponse(BaseModel):
    """OpenAPI response describing the available setup-time review modes."""

    policies: list[IngestionReviewPolicyOption]
    default: Literal["review_all", "review_ambiguous", "auto_write"]
    applies_to: list[str]


class IngestionProductLineOption(BaseModel):
    """A product line enabled in the active customer's saved ontology."""
    id: str = Field(..., description="Stable product-line ID. Submit this exact value.")
    name: str = Field(..., description="Customer-authoritative product-line display name.")


class IngestionDomainOption(BaseModel):
    """A domain enabled in the active customer's saved ontology."""
    id: str = Field(..., description="Stable domain ID. Submit this exact value.")
    name: str = Field(..., description="Customer-authoritative domain display name.")
    product_lines: list[IngestionProductLineOption] = Field(
        default_factory=list, description="Configured and applicable product lines."
    )


class IngestionCustomerOption(BaseModel):
    """Active customer workspace selectable before authorization is customer-aware."""
    customer_id: str
    customer_name: str
    industry_id: str


class IngestionCustomersResponse(BaseModel):
    """Active customer workspaces exposed to the authenticated context picker."""
    customers: list[IngestionCustomerOption]


class IngestionContextResponse(BaseModel):
    """Customer-bound ingestion choices for the Ingestion Studio bootstrap."""
    customer_id: str
    customer_name: str
    tenant_id: str
    industry_id: str
    customer_ontology_id: str
    customer_ontology_version: int
    domains: list[IngestionDomainOption]


class ScheduleJobRequest(BaseModel):
    """Request to schedule an ingestion job."""
    customer_id: str = Field(..., min_length=1, description="Explicit active customer workspace selected in Ingestion Studio")
    input_dir: str = Field(..., description="Path to directory containing documents")
    domain: str = Field(..., description="Configured domain ID returned by GET /ingestion/context")
    product_line_id: str = Field(..., min_length=1, description="Explicit configured product-line ID returned for the selected domain; never inferred from document content.")
    document_nature: Literal["qe_assets", "technology_artifacts"] = Field(..., description="UI lane classification: QE assets or technology artefacts. Both use the same ingestion pipeline.")
    force_rewrite: bool = Field(
        False,
        description=(
            "Rebuild only this customer/domain/product-line scope. Requires "
            "KBFABRIC_FORCE_REWRITE_ENABLED=true and the ingestion:force_rewrite permission."
        ),
    )
    hitl_mode: Literal["auto_write", "review_ambiguous", "review_all"] | None = Field(
        None, description="Review policy selected during ingestion setup"
    )
    project_ids: list[str] = Field(default_factory=list, description="Optional project metadata; not an authorization control.")
    context_tags: list[str] = Field(default_factory=list, description="Optional run context tags; not an authorization control.")
    scheduled_at: Optional[str] = Field(None, description="ISO 8601 timestamp for scheduled execution (e.g., 2025-12-15T14:30:00). If not provided, job runs immediately.")


class ScheduleJobWithFilesRequest(BaseModel):
    """Request to schedule an ingestion job with file uploads."""
    customer_id: str = Field(..., min_length=1, description="Explicit active customer workspace selected in Ingestion Studio")
    domain: str = Field(..., description="Configured domain ID returned by GET /ingestion/context")
    product_line_id: str = Field(..., min_length=1, description="Explicit configured product-line ID returned for the selected domain.")
    document_nature: Literal["qe_assets", "technology_artifacts"] = Field(..., description="UI lane classification. Both values use the same ingestion pipeline.")
    force_rewrite: bool = Field(
        False,
        description=(
            "Rebuild only this customer/domain/product-line scope. Requires "
            "KBFABRIC_FORCE_REWRITE_ENABLED=true and the ingestion:force_rewrite permission."
        ),
    )
    hitl_mode: Literal["auto_write", "review_ambiguous", "review_all"] | None = Field(
        None, description="Review policy selected during ingestion setup"
    )
    scheduled_at: Optional[str] = Field(None, description="ISO 8601 timestamp for scheduled execution")
    # Note: files are handled separately as UploadFile objects in the endpoint


class JobStatusResponse(BaseModel):
    """Job status response."""
    job_id: str
    status: str
    domain: Optional[str] = None
    input_dir: Optional[str] = None
    phase: Optional[str] = None
    progress_pct: Optional[int] = None
    files_done: Optional[int] = None
    files_total: Optional[int] = None
    blocks_extracted: Optional[int] = None
    entities_found: Optional[int] = None
    weaviate_chunks: Optional[int] = None
    neo4j_nodes: Optional[int] = None
    neo4j_relationships: Optional[int] = None
    elapsed_sec: Optional[float] = None
    error: Optional[str] = None
    warning_count: Optional[int] = None
    report_paths: Optional[dict] = None
    ray_dashboard_url: Optional[str] = None


class ScheduleJobResponse(BaseModel):
    """Response after scheduling a job."""
    job_id: str
    run_id: str | None = Field(None, description="Durable PostgreSQL audit record for this ingestion run")
    submitted_by: str | None = Field(None, description="Actor resolved by the API auth dependency")
    status: str
    message: str
    ray_dashboard_url: str



class JobListResponse(BaseModel):
    """Response containing list of jobs."""
    jobs: list[dict[str, Any]]
    total: int


class ResumeJobRequest(BaseModel):
    reason: str | None = Field(None, max_length=1000)


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    ray_connected: bool
    ray_dashboard: str
    active_jobs: int


# ============================================================
# Kubernetes Health Check Models
# ============================================================

class DependencyCheck(BaseModel):
    """Health status of a single dependency."""
    status: str = Field(..., description="healthy, unhealthy, or degraded")
    latency_ms: float | None = Field(None, description="Response time in milliseconds")
    error: str | None = Field(None, description="Error message if unhealthy")
    details: dict[str, Any] | None = Field(None, description="Additional details")


class LivenessResponse(BaseModel):
    """Kubernetes liveness probe response.
    
    Returns 200 if process is alive. Failing liveness causes container restart.
    """
    status: str = Field(..., description="alive or dead")
    timestamp: str = Field(..., description="ISO 8601 timestamp")
    uptime_seconds: float = Field(..., description="Process uptime in seconds")


class ReadinessResponse(BaseModel):
    """Kubernetes readiness probe response.
    
    Returns 200 only when Ray Jobs API is healthy and the service
    can accept job submissions. Failing readiness removes pod from Service endpoints.
    """
    status: str = Field(..., description="ready or not_ready")
    timestamp: str = Field(..., description="ISO 8601 timestamp")
    checks: dict[str, DependencyCheck] = Field(..., description="Per-dependency health status")
    ray_dashboard: str = Field(..., description="Ray dashboard URL")


# ============================================================
# Helper Functions
# ============================================================

def get_job_client() -> JobSubmissionClient:
    """Get or create Ray Jobs client."""
    global _job_client
    if _job_client is None:
        _job_client = JobSubmissionClient(RAY_DASHBOARD_URL)
    return _job_client


def read_progress_file(job_id: str) -> Optional[dict]:
    """Read job-specific progress file."""
    progress_file = PROGRESS_DIR / f"{job_id}.json"
    if progress_file.exists():
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def check_domain_lock(domain: str, customer_id: str | None = None) -> tuple[bool, str | None]:
    """
    Check if any job is currently ingesting the specified domain.
    
    Args:
        domain: Domain name to check
    
    Returns:
        Tuple of (is_locked, active_job_id)
        - is_locked: True if domain is being ingested
        - active_job_id: Job ID of active ingestion, or None
    """
    try:
        client = get_job_client()
        running_jobs = [
            j for j in client.list_jobs() 
            if j.status in [JobStatus.PENDING, JobStatus.RUNNING]
        ]
        
        for job in running_jobs:
            job_id = job.submission_id or job.job_id
            if not job_id:
                continue
            
            # Check progress file for domain
            progress = read_progress_file(job_id)
            if progress and progress.get("domain") == domain:
                running_customer_id = progress.get("customer_id")
                # Unknown-scope legacy jobs conservatively block this domain until they finish.
                if customer_id is None or running_customer_id in (None, customer_id):
                    return True, job_id
        
        return False, None
        
    except Exception as e:
        logger.warning(f"Failed to check domain lock: {e}")
        # Fail open - allow submission if check fails
        return False, None


def parse_progress_from_logs(job_id: str) -> Optional[dict]:
    """
    Parse progress information from Ray Job logs.
    
    Looks for [PROGRESS] lines and extracts structured data.
    Also extracts final results from JSON log lines.
    """
    try:
        client = get_job_client()
        logs = client.get_job_logs(job_id)
        
        progress = {}
        
        # Parse [PROGRESS] lines (format: [PROGRESS] Phase | pct% | Files: x/y | Blocks: z | Entities: e)
        for line in logs.split("\n"):
            if "[PROGRESS]" in line:
                try:
                    # Extract phase and percentage
                    parts = line.split("[PROGRESS]")[1].strip().split("|")
                    if len(parts) >= 2:
                        progress["phase"] = parts[0].strip()
                        pct_part = parts[1].strip()
                        if "%" in pct_part:
                            progress["progress_pct"] = int(pct_part.replace("%", "").strip())
                    
                    # Extract files
                    for part in parts:
                        if "Files:" in part:
                            files_str = part.split("Files:")[1].strip()
                            if "/" in files_str:
                                done, total = files_str.split("/")
                                progress["files_done"] = int(done.strip())
                                progress["files_total"] = int(total.strip())
                        if "Blocks:" in part:
                            progress["blocks_extracted"] = int(part.split("Blocks:")[1].strip())
                        if "Entities:" in part:
                            progress["entities_found"] = int(part.split("Entities:")[1].strip())
                except Exception:
                    pass
            
            # Parse final summary from JSON log line
            if '"event": "Ingestion completed' in line and '"summary":' in line:
                try:
                    # Find the JSON object in the line
                    json_start = line.find("{")
                    if json_start >= 0:
                        log_data = json.loads(line[json_start:])
                        summary = log_data.get("summary", {})
                        progress.update({
                            "domain": summary.get("domain"),
                            "input_dir": summary.get("input_dir"),
                            "files_total": summary.get("total_files"),
                            "blocks_extracted": summary.get("total_blocks"),
                            "entities_found": summary.get("entities_extracted"),
                            "weaviate_chunks": summary.get("weaviate_chunks"),
                            "neo4j_nodes": summary.get("neo4j_nodes"),
                            "neo4j_relationships": summary.get("neo4j_relationships"),
                            "report_paths": summary.get("report_paths")
                        })
                except Exception:
                    pass
        
        return progress if progress else None
        
    except Exception:
        return None


def get_job_status_from_ray(job_id: str) -> Optional[str]:
    """Get job status from Ray Jobs API."""
    try:
        client = get_job_client()
        status = client.get_job_status(job_id)
        return status.value if status else None
    except Exception:
        return None


# ============================================================
# Lifespan
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    print("[API] Starting up...", flush=True)

    # Initialize Fabric Foundry control-plane state before the API
    # is considered ready.
    try:
        FabricFoundryBootstrap().run()
        print("[API] Fabric Foundry bootstrap completed.", flush=True)
    except Exception as e:
        print(f"[API] ERROR: Fabric Foundry bootstrap failed: {e}", flush=True)
        raise

    # Initialize OpenTelemetry
    setup_telemetry()
    #instrument_fastapi(app)
    
    # Load embedding model at startup (fail-fast)
    try:
        config = get_app_config()
        embeddings_cfg = config.get("embeddings", {})
        model_name = embeddings_cfg.get("model_name", "unknown")
        print(f"[API] Loading embedding model: {model_name}", flush=True)
        embeddings = EmbeddingsClient(config)
        embeddings.load_model()
        print(f"[API] Embedding model loaded successfully: {model_name}", flush=True)
    except Exception as e:
        print(f"[API] ERROR: Failed to load embedding model: {e}", flush=True)
        raise
    
    # Verify Ray Jobs API is accessible
    try:
        client = get_job_client()
        jobs = client.list_jobs()
        print(f"[API] Ray Jobs API connected. {len(jobs)} existing jobs.", flush=True)
    except Exception as e:
        print(f"[API] WARNING: Ray Jobs API not available: {e}", flush=True)
        print(f"[API] Make sure Ray is running: ray start --head", flush=True)
    
    print(f"[API] Ready. Dashboard: {RAY_DASHBOARD_PUBLIC_URL} (client: {RAY_DASHBOARD_URL})", flush=True)
    
    retry_task = None
    from kbfabric_ingest.ingest.retry_policy import automatic_retry_enabled
    if automatic_retry_enabled():
        retry_task = asyncio.create_task(_automatic_retry_supervisor())
        logger.info("ingestion.auto_retry.enabled")
    yield
    if retry_task is not None:
        retry_task.cancel()
        try:
            await retry_task
        except asyncio.CancelledError:
            pass
    
    print("[API] Shutting down...", flush=True)


async def _automatic_retry_supervisor() -> None:
    """Retry eligible transient failures through the normal validated resume path."""
    from kbfabric_ingest.ingest.retry_policy import (
        automatic_retry_max_attempts, automatic_retry_poll_seconds,
    )
    repository = IngestionRunRepository()
    while True:
        try:
            candidates = await asyncio.to_thread(
                repository.list_auto_retry_candidates,
                max_attempts=automatic_retry_max_attempts(), limit=50,
            )
            for candidate in candidates:
                run_id = candidate["run_id"]
                retry_number = int(candidate.get("auto_retry_count", 0)) + 1
                retry_key = f"auto-retry-{run_id}-{retry_number}"
                system_actor = AuthenticatedActor(
                    "system:auto-retry", "internal",
                    frozenset({"ingestion:jobs:resume_any", "ingestion:jobs:auto_retry"}),
                )
                try:
                    await resume_ingestion_job(
                        candidate["job_id"],
                        ResumeJobRequest(reason="Bounded automatic retry after a transient failure"),
                        retry_key, system_actor,
                    )
                except HTTPException as exc:
                    logger.warning(
                        "ingestion.auto_retry.not_dispatched",
                        job_id=candidate["job_id"], status_code=exc.status_code,
                    )
                    if exc.status_code in {404, 409}:
                        await asyncio.to_thread(
                            repository.suppress_auto_retry,
                            run_id, reason=str(exc.detail),
                        )
                except Exception as exc:  # keep supervisor alive if one candidate is malformed
                    logger.exception(
                        "ingestion.auto_retry.dispatch_error",
                        job_id=candidate["job_id"], error=str(exc),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("ingestion.auto_retry.poll_error", error=str(exc))
        await asyncio.sleep(automatic_retry_poll_seconds())


# ============================================================
# FastAPI App
# ============================================================


async def platform_validation_exception_handler(
    request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return request validation failures using the platform error contract."""

    from kbfabric_ingest.utils.api_errors import validation_error_response

    return JSONResponse(
        status_code=400,
        content=validation_error_response(exc),
    )


app = FastAPI(
    title="Pulse Knowledge Fabric - Ingestion API",
    version="2.0.0",
    description="Schedule and monitor parallel document ingestion via Ray Jobs",
    lifespan=lifespan
)

app.add_exception_handler(
    RequestValidationError,
    platform_validation_exception_handler,
)

# Add middleware FIRST (before instrumentation to avoid "app already started" errors)
# CORS origins from KBFABRIC_CORS_ORIGINS in .env (required; use "*" for all).
_cors_origins = [
    o.strip() for o in require_env("KBFABRIC_CORS_ORIGINS").split(",") if o.strip()
]
if not _cors_origins:
    raise ValueError("KBFABRIC_CORS_ORIGINS must list at least one origin (or '*')")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instrument FastAPI AFTER middleware (must be outside lifespan)
instrument_fastapi(app)

# Include security router for InfoSec compliance reporting
app.include_router(security_router)
app.include_router(fabric_foundry_router)
app.include_router(hitl_router)


# ============================================================
# Endpoints
# ============================================================

@app.get(
    "/ingestion/review-policies",
    tags=["Ingestion Configuration"],
    response_model=IngestionReviewPolicyResponse,
    summary="Get available ingestion review policies",
    description=(
        "Call during ingestion setup to populate the Review policy selector. "
        "Submit the selected `value` with the job request; it is frozen for that run. "
        "This configuration endpoint is separate from post-pipeline HITL review APIs."
    ),
)
async def list_ingestion_review_policies() -> IngestionReviewPolicyResponse:
    """Return review choices for the Ingestion Studio setup screen."""
    return {
        "policies": [
            {
                "value": "review_all",
                "label": "Review everything",
                "description": "Review every eligible node and relationship decision before KG admission.",
            },
            {
                "value": "review_ambiguous",
                "label": "Review ambiguous",
                "description": "Automatically admit eligible high-confidence decisions; queue ambiguous decisions.",
            },
            {
                "value": "auto_write",
                "label": "Skip review",
                "description": "Automatically admit eligible decisions; retain hard ontology and scope constraints.",
            },
        ],
        "default": get_app_config().get("ingestion", {}).get(
            "hitl_mode", "review_ambiguous"
        ),
        "applies_to": ["node_admission", "triplet_admission"],
    }

@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health():
    """Basic health check (legacy) - always responds instantly."""
    ray_connected = False
    active_jobs = 0
    
    try:
        client = get_job_client()
        jobs = client.list_jobs()
        ray_connected = True
        active_jobs = sum(1 for j in jobs if j.status in [JobStatus.PENDING, JobStatus.RUNNING])
    except Exception:
        pass
    
    return HealthResponse(
        status="healthy" if ray_connected else "degraded",
        ray_connected=ray_connected,
        ray_dashboard=RAY_DASHBOARD_PUBLIC_URL,
        active_jobs=active_jobs
    )


@app.get("/health/live", response_model=LivenessResponse, tags=["Health"])
async def liveness_probe():
    """
    Kubernetes liveness probe.
    
    Returns 200 if the process is alive. This is a lightweight check that
    does NOT verify Ray or other dependencies - it only confirms the process is running.
    
    Failing this probe will cause Kubernetes to restart the container.
    
    Use case: Detect deadlocks, infinite loops, or crashed processes.
    """
    uptime = time.time() - _startup_time
    
    return LivenessResponse(
        status="alive",
        timestamp=datetime.now(timezone.utc).isoformat(),
        uptime_seconds=round(uptime, 2)
    )


@app.get("/health/ready", response_model=ReadinessResponse, tags=["Health"])
async def readiness_probe():
    """
    Kubernetes readiness probe.
    
    Returns 200 only when Ray Jobs API is healthy and the service can accept
    job submissions. Performs actual connectivity check against Ray cluster.
    
    Failing this probe will remove the pod from Service endpoints,
    stopping traffic until Ray becomes available.
    
    Use case: Graceful startup/shutdown, Ray cluster unavailable.
    """
    checks: dict[str, DependencyCheck] = {}
    all_healthy = True
    
    # Check Ray Jobs API connectivity
    try:
        start = time.time()
        client = get_job_client()
        jobs = client.list_jobs()
        latency = (time.time() - start) * 1000
        
        active_jobs = sum(1 for j in jobs if j.status in [JobStatus.PENDING, JobStatus.RUNNING])
        completed_jobs = sum(1 for j in jobs if j.status == JobStatus.SUCCEEDED)
        failed_jobs = sum(1 for j in jobs if j.status == JobStatus.FAILED)
        
        checks["ray_jobs_api"] = DependencyCheck(
            status="healthy",
            latency_ms=round(latency, 2),
            details={
                "total_jobs": len(jobs),
                "active_jobs": active_jobs,
                "completed_jobs": completed_jobs,
                "failed_jobs": failed_jobs
            }
        )
    except Exception as e:
        checks["ray_jobs_api"] = DependencyCheck(
            status="unhealthy",
            error=str(e)[:200]
        )
        all_healthy = False
    
    # Check progress directory is writable
    try:
        test_file = PROGRESS_DIR / ".health_check"
        test_file.write_text("health")
        test_file.unlink()
        checks["progress_dir"] = DependencyCheck(status="healthy")
    except Exception as e:
        checks["progress_dir"] = DependencyCheck(
            status="unhealthy",
            error=f"Progress directory not writable: {e}"
        )
        all_healthy = False
    
    # Check reports directory is writable
    try:
        test_file = REPORTS_DIR / ".health_check"
        test_file.write_text("health")
        test_file.unlink()
        checks["reports_dir"] = DependencyCheck(status="healthy")
    except Exception as e:
        checks["reports_dir"] = DependencyCheck(
            status="unhealthy",
            error=f"Reports directory not writable: {e}"
        )
        all_healthy = False

    # Check a Python venv exists for Ray jobs (.venv_ray or .venv)
    venv_ok = False
    for venv_name in (".venv_ray", ".venv"):
        venv_dir = BASE_DIR / venv_name
        python_bin = venv_dir / "bin" / "python"
        if venv_dir.exists() and python_bin.exists():
            venv_ok = True
            checks["venv_ray"] = DependencyCheck(
                status="healthy",
                details={"venv": venv_name}
            )
            break
    if not venv_ok:
        checks["venv_ray"] = DependencyCheck(
            status="unhealthy",
            error=f"Neither .venv_ray nor .venv found. {VENV_RAY_SETUP_REMINDER}"
        )
        all_healthy = False

    # Check ontology configuration is loaded
    try:
        from kbfabric_ingest._common.config import get_domains_full
        domains = get_domains_full()
        if domains:
            checks["ontology"] = DependencyCheck(
                status="healthy",
                details={"domain_count": len(domains)}
            )
        else:
            checks["ontology"] = DependencyCheck(
                status="unhealthy",
                error="Ontology loaded but no domains found"
            )
            all_healthy = False
    except Exception as e:
        checks["ontology"] = DependencyCheck(
            status="unhealthy",
            error=f"Ontology configuration failed to load: {str(e)[:200]}"
        )
        all_healthy = False

    # Weaviate and Neo4j connectivity (shared helpers; config from get_app_config())
    config = get_app_config()
    weaviate_url = (config.get("weaviate") or {}).get("url")
    start = time.time()
    weaviate_ok, weaviate_err = check_weaviate(config)
    weaviate_latency_ms = (time.time() - start) * 1000
    if weaviate_ok:
        checks["weaviate"] = DependencyCheck(
            status="healthy",
            latency_ms=round(weaviate_latency_ms, 2),
            details={"url": weaviate_url or "unknown"},
        )
    else:
        checks["weaviate"] = DependencyCheck(
            status="unhealthy",
            error=weaviate_err or "Connection failed",
            details={"url": weaviate_url} if weaviate_url else None,
        )
        all_healthy = False

    neo4j_uri = (config.get("neo4j") or {}).get("uri")
    start = time.time()
    neo4j_ok, neo4j_err = check_neo4j(config)
    neo4j_latency_ms = (time.time() - start) * 1000
    if neo4j_ok:
        checks["neo4j"] = DependencyCheck(
            status="healthy",
            latency_ms=round(neo4j_latency_ms, 2),
            details={"uri": neo4j_uri or "unknown"},
        )
    else:
        checks["neo4j"] = DependencyCheck(
            status="unhealthy",
            error=neo4j_err or "Connection failed",
            details={"uri": neo4j_uri} if neo4j_uri else None,
        )
        all_healthy = False

    status = "ready" if all_healthy else "not_ready"
    
    response = ReadinessResponse(
        status=status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        checks=checks,
        ray_dashboard=RAY_DASHBOARD_PUBLIC_URL
    )
    
    # Return 503 if not ready
    if not all_healthy:
        raise HTTPException(
            status_code=503,
            detail=response.model_dump()
        )
    
    return response


def _require_force_rewrite_permission(actor: AuthenticatedActor) -> None:
    """Enforce the environment kill switch and caller permission for scoped rewrite."""
    if not get_app_config().get("ingestion", {}).get("force_rewrite_enabled", False):
        raise HTTPException(
            status_code=403,
            detail="Force rewrite is disabled by KBFABRIC_FORCE_REWRITE_ENABLED.",
        )
    if "ingestion:force_rewrite" not in actor.permissions:
        raise HTTPException(
            status_code=403,
            detail="Caller lacks the ingestion:force_rewrite permission.",
        )


@app.post(
    "/jobs",
    response_model=ScheduleJobResponse,
    tags=["Ingestion Jobs"],
    summary="Submit an ingestion job from a server-side input directory",
    description=(
        "Submit an ingestion run through Ray Jobs. Select an active customer from "
        "GET /ingestion/customers, then call GET /ingestion/context?customer_id=...; "
        "send its configured domain ID and an applicable product_line_id explicitly. "
        "Choose document_nature (qe_assets or technology_artifacts) for UI organization; "
        "both use the same ingestion service. Select hitl_mode from GET /ingestion/review-policies. "
        "The API validates the scope against the active Customer Ontology before submitting. "
        "A successful response returns a job_id for GET /jobs/{job_id} and /progress/{job_id}. "
        "Force rewrite is disabled by default and requires an explicit permission; "
        "when enabled, it rebuilds only the selected customer/domain/product-line scope. "
        "scheduled_at delays execution using the existing scheduler support."
    ),
    responses={
        403: {"description": "Force rewrite is disabled or caller lacks its permission."},
        409: {"description": "Scope lock conflict or global schema fingerprint requires a coordinated migration."},
        422: {"description": "Domain or product line is not configured and applicable for the active customer."},
    },
)
async def schedule_job(
    request: ScheduleJobRequest,
    actor: AuthenticatedActor = Depends(require_authenticated_actor),
):
    """Submit a customer-scoped ingestion job via Ray Jobs."""
    import asyncio
    
    try:
        if request.force_rewrite:
            _require_force_rewrite_permission(actor)
        _ensure_venv_ray_ready()
        frozen_context = _validate_ingestion_selection(request.customer_id, request.domain, request.product_line_id)
        client = get_job_client()
        
        # Calculate delay if scheduled_at is provided
        delay_seconds = 0
        schedule_msg = ""
        if request.scheduled_at:
            try:
                # Parse scheduled time (supports ISO 8601 format)
                scheduled_time = datetime.fromisoformat(request.scheduled_at.replace("Z", "+00:00"))
                if scheduled_time.tzinfo is None:
                    # Assume local timezone if not specified
                    scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
                
                now = datetime.now(timezone.utc)
                delay_seconds = max(0, (scheduled_time - now).total_seconds())
                
                if delay_seconds > 0:
                    # Cap delay at 1 hour - for longer delays, recommend external scheduler
                    if delay_seconds > 3600:
                        raise HTTPException(
                            status_code=400, 
                            detail=f"Scheduled time too far in future ({delay_seconds/3600:.1f} hours). Max delay is 1 hour. Use external scheduler for longer delays."
                        )
                    schedule_msg = f" [SCHEDULED: {scheduled_time.strftime('%Y-%m-%d %H:%M:%S UTC')}]"
                    print(f"[API] Job scheduled for {scheduled_time}, delay={delay_seconds:.0f}s", flush=True)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid scheduled_at format: {e}. Use ISO 8601 format (e.g., 2025-12-15T14:30:00)")
        
        # Check for max concurrent jobs limit (prevents resource exhaustion)
        config = get_app_config()
        max_concurrent = config["scheduler"]["max_concurrent_jobs"]
        try:
            running_jobs = [
                j for j in client.list_jobs() 
                if j.status in [JobStatus.PENDING, JobStatus.RUNNING]
            ]
            if len(running_jobs) >= max_concurrent:
                raise HTTPException(
                    status_code=429,
                    detail=f"Max concurrent jobs ({max_concurrent}) reached. {len(running_jobs)} jobs currently running. Try again later or wait for jobs to complete."
                )
            print(f"[API] Concurrent job check: {len(running_jobs)}/{max_concurrent} active", flush=True)
        except HTTPException:
            raise
        except Exception as e:
            # Log but don't block if check fails
            print(f"[API] Warning: Could not check running jobs: {e}", flush=True)
        
        # Check domain-level lock (one ingestion per domain at a time)
        is_locked, active_job_id = check_domain_lock(request.domain, request.customer_id)
        if is_locked:
            raise HTTPException(
                status_code=409,  # Conflict
                detail=(
                    f"Domain '{request.domain}' ingestion already in progress (job: {active_job_id}). "
                    f"Wait for completion or stop with: ray job stop {active_job_id}"
                )
            )
        
        # A fabric-wide fingerprint mismatch requires a coordinated migration.
        try:
            stored = get_stored_fingerprint()
            current = compute_fingerprint()
            if stored is not None and stored != current:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The fabric-wide schema/ontology/index fingerprint changed. "
                        "A product-line rewrite cannot migrate every affected scope; "
                        "run a coordinated migration before submitting ingestion."
                    ),
                )
        except HTTPException:
            raise
        except Exception as e:
            print(f"[API] Fingerprint check failed (allowing job): {e}", flush=True)
        
        # Generate job ID
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        job_id = f"ingest-{request.domain}-{timestamp}-{uuid.uuid4().hex[:4]}"
        run_id = str(uuid.uuid4())
        run_repository = IngestionRunRepository()
        
        # Resolve input_dir: Ray job runs from extracted zip (excludes drop_zone, uploads).
        # Use absolute path under KBFABRIC_INGEST_PERSIST_DIR so job reads from mounted volume.
        persist_dir = (
            (os.environ.get("KBFABRIC_INGEST_PERSIST_DIR") or "").strip()
            or str(BASE_DIR)
        )
        input_dir_raw = request.input_dir
        if input_dir_raw.startswith("/mnt/") or input_dir_raw.startswith("/home/") or input_dir_raw.startswith("/"):
            input_path = Path(input_dir_raw)
        else:
            input_path = Path(input_dir_raw)
            if not input_path.is_absolute():
                input_path = BASE_DIR / input_path
            input_path = input_path.resolve()
        ray_dir = _resolve_ray_worker_dir(persist_dir)
        if input_path.is_relative_to(BASE_DIR):
            relative_path = str(input_path.relative_to(BASE_DIR))
            input_dir_for_job = f"{ray_dir.rstrip('/')}/{relative_path}"
        else:
            input_dir_for_job = str(input_path)

        source_manifest = _build_directory_source_manifest(input_path, input_dir_for_job)
        persisted_timestamps = run_repository.create(_ingestion_run_record(
            run_id=run_id, job_id=job_id, actor=actor, frozen_context=frozen_context,
            domain_id=request.domain, product_line_id=request.product_line_id,
            document_nature=request.document_nature, hitl_mode=request.hitl_mode,
            force_rewrite_requested=request.force_rewrite,
            submission_metadata={"project_ids": request.project_ids, "context_tags": request.context_tags},
            source_manifest=source_manifest,
        ))
        worker_audit_context = _attach_submission_metadata({
            **frozen_context,
            **persisted_timestamps,
            "run_id": run_id,
            "job_id": job_id,
            "ingestion_batch_id": job_id,
            "submitted_by": actor.user_id,
            "auth_source": actor.auth_source,
            "document_nature": request.document_nature,
            "hitl_mode": request.hitl_mode,
            "force_rewrite_requested": request.force_rewrite,
        }, {"project_ids": request.project_ids, "context_tags": request.context_tags})

        # Build entrypoint command with optional delay (explicit Python from .venv_ray)
        python_path = _get_ray_python_path(persist_dir)
        entrypoint_parts = []
        delay_prefix = None
        if delay_seconds > 0:
            delay_prefix = (
                f"{shlex.quote(python_path)} -c \"import time; "
                f"print('[SCHEDULER] Waiting {int(delay_seconds)}s until scheduled time...'); "
                f"time.sleep({int(delay_seconds)})\" &&"
            )
        script_path = f"{ray_dir.rstrip('/')}/scripts/run_ingestion_job.py"
        entrypoint_parts.extend([
            python_path, script_path,
            "--job-id", job_id,
            "--run-id", run_id,
            "--submitted-by", actor.user_id,
            "--auth-source", actor.auth_source,
            "--customer-id", request.customer_id,
            "--customer-ontology-version", str(frozen_context["customer_ontology_version"]),
            "--domain", request.domain,
            "--product-line-id", request.product_line_id,
            "--document-nature", request.document_nature,
            "--input-dir", input_dir_for_job,
        ])
        if request.hitl_mode is not None:
            entrypoint_parts.extend(["--hitl-mode", request.hitl_mode])
        if request.force_rewrite:
            entrypoint_parts.append("--force-rewrite")
        entrypoint = shlex.join(str(p) for p in entrypoint_parts)
        if delay_prefix:
            entrypoint = f"{delay_prefix} {entrypoint}"

        # Add 10-second breathing room before submitting to Ray
        await asyncio.sleep(10)
        print(f"[API] Job submission delay completed for {job_id}", flush=True)

        # Submit to Ray Jobs (no pip - uses .venv_ray via full path in entrypoint)
        try:
            submission_id = client.submit_job(
                entrypoint=entrypoint,
                submission_id=job_id,
                runtime_env=_get_ray_runtime_env(
                    persist_dir, config, audit_context=worker_audit_context
                )
            )
        except Exception as submit_error:
            run_repository.update_status(
                run_id, "FAILED", error_message=f"Ray dispatch failed: {submit_error}"
            )
            raise
        
        print(f"[API] Job submitted: {job_id}{schedule_msg}", flush=True)
        
        # Record Ray job submission telemetry
        record_ray_job_submitted(request.domain)
        
        force_msg = " [FORCE REWRITE]" if request.force_rewrite else ""
        return ScheduleJobResponse(
            job_id=job_id,
            run_id=run_id,
            submitted_by=actor.user_id,
            status="submitted" if delay_seconds == 0 else "scheduled",
            message=f"Job submitted for {request.domain}{force_msg}{schedule_msg}",
            ray_dashboard_url=f"{RAY_DASHBOARD_PUBLIC_URL}/#/jobs/{job_id}"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[API] Job submission failed: {e}", flush=True)
        raise HTTPException(status_code=500, detail=f"Failed to schedule job: {str(e)}")


@app.post(
    "/jobs/upload",
    response_model=ScheduleJobResponse,
    tags=["Ingestion Jobs"],
    summary="Upload files and submit an ingestion job",
    description=(
        "Multipart upload for the Ingestion Studio. Select an active customer from "
        "GET /ingestion/customers; obtain domain and product-line IDs from "
        "GET /ingestion/context?customer_id=... and review policy from GET /ingestion/review-policies. "
        "document_nature labels the run as QE assets or technology artefacts; both run through "
        "the same ingestion pipeline. The selected product line is validated against the "
        "active customer's persisted ontology and is never inferred from uploaded content. "
        "Force rewrite is disabled by default and requires an explicit permission; when "
        "enabled, it rebuilds only this customer/domain/product-line scope. "
        "Returns a job_id and status; monitor with GET /jobs/{job_id}."
    ),
    responses={
        403: {"description": "Force rewrite is disabled or caller lacks its permission."},
        409: {"description": "Scope lock conflict or global schema fingerprint requires a coordinated migration."},
        422: {"description": "Domain or product line is not configured and applicable for the active customer."},
    },
)
async def schedule_job_with_files(
    customer_id: str = Form(..., min_length=1, description="Active customer ID selected in Ingestion Studio"),
    domain: str = Form(..., description="Configured domain ID from GET /ingestion/context?customer_id=..."),
    product_line_id: str = Form(..., description="Explicit configured product-line ID from the selected domain"),
    document_nature: Literal["qe_assets", "technology_artifacts"] = Form(..., description="UI lane classification; both values use the same ingestion service"),
    force_rewrite: bool = Form(
        False,
        description=(
            "Rebuild only this customer/domain/product-line scope; requires the "
            "environment kill switch and ingestion:force_rewrite permission."
        ),
    ),
    hitl_mode: Literal["auto_write", "review_ambiguous", "review_all"] | None = Form(None),
    project_ids: list[str] = Form(default=[], description="Optional project metadata; not an authorization control."),
    context_tags: list[str] = Form(default=[], description="Optional context tags; not an authorization control."),
    files: List[UploadFile] = File(..., description="Files to ingest (max 50 files, 100MB each)"),
    actor: AuthenticatedActor = Depends(require_authenticated_actor),
):
    """
    Submit a new ingestion job with file uploads via Ray Jobs.

    Upload files directly through the API instead of requiring a pre-existing directory.
    Files are temporarily stored and automatically cleaned up after processing.
    Jobs start immediately upon submission.

    - **domain**: Configured domain ID returned by GET /ingestion/context
    - **product_line_id**: Explicit applicable product-line ID nested under the selected domain
    - **document_nature**: `qe_assets` or `technology_artifacts`; both use the same service
    - **hitl_mode**: `review_all`, `review_ambiguous`, or `auto_write`
    - **force_rewrite**: Rebuild only the selected customer/domain/product-line scope; guarded by environment and permission
    - **files**: Multiple file uploads (DOCX, PDF, XLSX, TXT, CSV supported)

    Limits:
    - Maximum 50 files per request
    - Maximum 100MB per individual file
    - Total request size limited by HTTP server

    Returns job ID and status. Monitor progress via /jobs/{job_id}.
    """
    try:
        if force_rewrite:
            _require_force_rewrite_permission(actor)
        _ensure_venv_ray_ready()
        config = get_app_config()
        frozen_context = _validate_ingestion_selection(customer_id, domain, product_line_id)

        # Check domain-level lock (one ingestion per domain at a time)
        is_locked, active_job_id = check_domain_lock(domain, customer_id)
        if is_locked:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Domain '{domain}' ingestion already in progress (job: {active_job_id}). "
                    f"Wait for completion or stop with: ray job stop {active_job_id}"
                )
            )

        # A fabric-wide fingerprint mismatch requires a coordinated migration.
        try:
            stored = get_stored_fingerprint()
            current = compute_fingerprint()
            if stored is not None and stored != current:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The fabric-wide schema/ontology/index fingerprint changed. "
                        "A product-line rewrite cannot migrate every affected scope; "
                        "run a coordinated migration before submitting ingestion."
                    ),
                )
        except HTTPException:
            raise
        except Exception as e:
            print(f"[API] Fingerprint check failed (allowing job): {e}", flush=True)

        # Validate file count
        if len(files) == 0:
            raise HTTPException(status_code=400, detail="At least one file must be provided")

        if len(files) > 50:
            raise HTTPException(status_code=400, detail="Maximum 50 files per request")

        # Validate files
        allowed_extensions = {'.docx', '.pdf', '.xlsx', '.txt', '.csv', '.pptx'}
        total_size = 0

        for file in files:
            # Check file extension
            ext = Path(file.filename).suffix.lower()
            if ext not in allowed_extensions:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported file type '{ext}' for '{file.filename}'. Allowed: {', '.join(allowed_extensions)}"
                )

            # Check file size (100MB limit)
            if hasattr(file, 'size') and file.size > 100_000_000:  # 100MB
                raise HTTPException(
                    status_code=400,
                    detail=f"File '{file.filename}' too large ({file.size/1_000_000:.1f}MB). Maximum 100MB per file."
                )

            # Accumulate total size for logging
            total_size += getattr(file, 'size', 0)

        logger.info(f"Upload request: {len(files)} files, {total_size/1_000_000:.1f}MB total, domain={domain}")

        # Generate unique job ID
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        job_id = f"ingest-{domain}-{timestamp}-{uuid.uuid4().hex[:4]}"
        run_id = str(uuid.uuid4())
        run_repository = IngestionRunRepository()

        # Create job-specific upload directory
        job_upload_dir = create_job_upload_dir(job_id)

        # Save uploaded files to job directory (spaces -> underscores for LibreOffice compatibility)
        saved_files = []
        source_files = []
        for file in files:
            safe_filename = file.filename.replace(" ", "_")
            file_path = job_upload_dir / safe_filename
            try:
                with open(file_path, "wb") as f:
                    shutil.copyfileobj(file.file, f)
                saved_files.append(safe_filename)
                digest = hashlib.sha256()
                with open(file_path, "rb") as saved_file:
                    for chunk in iter(lambda: saved_file.read(1024 * 1024), b""):
                        digest.update(chunk)
                source_files.append({
                    "name": safe_filename,
                    "size_bytes": file_path.stat().st_size,
                    "sha256": digest.hexdigest(),
                })
                logger.debug(f"Saved file: {file.filename} -> {file_path}")
            except Exception as e:
                logger.error(f"Failed to save file {file.filename}: {e}")
                # Cleanup partial saves
                cleanup_upload_dir(job_upload_dir)
                raise HTTPException(status_code=500, detail=f"Failed to save file {file.filename}: {str(e)}")

        logger.info(f"Saved {len(saved_files)} files for job {job_id}: {saved_files}")
        persist_dir = (
            (os.environ.get("KBFABRIC_INGEST_PERSIST_DIR") or "").strip()
            or str(BASE_DIR)
        )
        ray_dir = _resolve_ray_worker_dir(persist_dir)
        relative_path = (
            str(job_upload_dir.relative_to(BASE_DIR))
            if job_upload_dir.is_relative_to(BASE_DIR)
            else str(job_upload_dir)
        )
        input_dir_for_job = f"{ray_dir.rstrip('/')}/{relative_path}"
        upload_manifest_digest = hashlib.sha256(
            json.dumps(source_files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        persisted_timestamps = run_repository.create(_ingestion_run_record(
            run_id=run_id, job_id=job_id, actor=actor, frozen_context=frozen_context,
            domain_id=domain, product_line_id=product_line_id,
            document_nature=document_nature, hitl_mode=hitl_mode,
            force_rewrite_requested=force_rewrite,
            submission_metadata={"project_ids": project_ids, "context_tags": context_tags},
            source_manifest={
                "kind": "upload", "input_dir": str(job_upload_dir),
                "worker_input_dir": input_dir_for_job, "files": source_files,
                "inventory_complete": True, "manifest_sha256": upload_manifest_digest,
            },
        ))
        worker_audit_context = _attach_submission_metadata({
            **frozen_context,
            **persisted_timestamps,
            "run_id": run_id,
            "job_id": job_id,
            "ingestion_batch_id": job_id,
            "submitted_by": actor.user_id,
            "auth_source": actor.auth_source,
            "document_nature": document_nature,
            "hitl_mode": hitl_mode,
            "force_rewrite_requested": force_rewrite,
        }, {"project_ids": project_ids, "context_tags": context_tags})

        # Submit Ray job immediately
        client = get_job_client()

        # Ray job runs from extracted zip (excludes uploads). Use absolute path under
        # KBFABRIC_INGEST_PERSIST_DIR so job reads from mounted volume.
        python_path = _get_ray_python_path(persist_dir)
        script_path = f"{ray_dir.rstrip('/')}/scripts/run_ingestion_job.py"
        entrypoint_parts = [
            python_path, script_path,
            "--job-id", job_id,
            "--run-id", run_id,
            "--submitted-by", actor.user_id,
            "--auth-source", actor.auth_source,
            "--customer-id", customer_id,
            "--customer-ontology-version", str(frozen_context["customer_ontology_version"]),
            "--domain", domain,
            "--product-line-id", product_line_id,
            "--document-nature", document_nature,
            "--input-dir", input_dir_for_job,
        ]
        if hitl_mode is not None:
            entrypoint_parts.extend(["--hitl-mode", hitl_mode])
        if force_rewrite:
            entrypoint_parts.append("--force-rewrite")
        entrypoint = shlex.join(str(p) for p in entrypoint_parts)

        # Add 10-second breathing room before submitting to Ray
        await asyncio.sleep(10)
        print(f"[API] Upload job submission delay completed for {job_id}", flush=True)

        # Submit to Ray (no pip - uses .venv_ray via full path in entrypoint)
        try:
            submission_id = client.submit_job(
                entrypoint=entrypoint,
                submission_id=job_id,
                runtime_env=_get_ray_runtime_env(
                    persist_dir, config, audit_context=worker_audit_context
                ),
            )
        except Exception as submit_error:
            run_repository.update_status(
                run_id, "FAILED", error_message=f"Ray dispatch failed: {submit_error}"
            )
            raise
        # Schedule cleanup (runs in background)
        # Respect artifacts.mute_ray_deletion config
        if not config["artifacts"]["mute_ray_deletion"]:
            schedule_cleanup(job_id, job_upload_dir)
        else:
            logger.info(f"Artifact cleanup muted for job {job_id} (debugging/audit preservation)")

        # Record telemetry
        record_ray_job_submitted(domain)

        force_msg = " [FORCE REWRITE]" if force_rewrite else ""
        logger.info(f"Upload job submitted: {job_id}, {len(saved_files)} files, domain={domain}{force_msg}")

        return ScheduleJobResponse(
            job_id=job_id,
            run_id=run_id,
            submitted_by=actor.user_id,
            status="submitted",
            message=f"Upload job submitted for {domain} ({len(saved_files)} files){force_msg}",
            ray_dashboard_url=f"{RAY_DASHBOARD_PUBLIC_URL}/#/jobs/{job_id}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload job submission failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to schedule upload job: {str(e)}")


@app.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    status: str | None = None, customer_id: str | None = None,
    domain_id: str | None = None, product_line_id: str | None = None,
    actor: AuthenticatedActor = Depends(require_authenticated_actor),
):
    """List durable jobs submitted by this actor or all jobs for an authorized operator."""
    try:
        jobs, total = await asyncio.to_thread(
            IngestionRunRepository().list_jobs,
            submitted_by=actor.user_id,
            read_all="ingestion:jobs:read_all" in actor.permissions,
            limit=limit, offset=offset, status=status, customer_id=customer_id,
            domain_id=domain_id, product_line_id=product_line_id,
        )
        return JobListResponse(jobs=jobs, total=total)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list jobs: {str(e)}")


@app.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    actor: AuthenticatedActor = Depends(require_authenticated_actor),
):
    """Get the durable run snapshot for its submitter or an authorized jobs administrator."""
    try:
        details = await asyncio.to_thread(IngestionRunRepository().get_job_details, job_id)
        if details is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if details["submitted_by"] != actor.user_id and "ingestion:jobs:read_all" not in actor.permissions:
            raise HTTPException(status_code=404, detail="Job not found")
        if details.get("resume_available"):
            repository = IngestionRunRepository()
            record = await asyncio.to_thread(repository.get_resume_record, job_id)
            checkpoint = await asyncio.to_thread(repository.get_latest_checkpoint, details["run_id"])
            try:
                from kbfabric_ingest.ingest.checkpoints import read_checkpoint_artifact, stable_digest
                if not record or not checkpoint:
                    raise ValueError("Checkpoint metadata is missing")
                source_manifest = record.get("source_manifest_json") or {}
                frozen_context = record.get("run_context_json") or {}
                artifact = Path(checkpoint["artifact_uri"]).resolve()
                root = (Path(os.environ.get("KBFABRIC_INGEST_PERSIST_DIR") or BASE_DIR)
                        / "checkpoints" / details["run_id"]).resolve()
                if (not artifact.is_relative_to(root)
                        or checkpoint.get("source_manifest_sha256") != stable_digest(source_manifest)
                        or checkpoint.get("context_sha256") != stable_digest(frozen_context)
                        or await asyncio.to_thread(_verify_resume_source_manifest, source_manifest) is None):
                    raise ValueError("Checkpoint provenance is no longer valid")
                stage, checkpoint_payload = read_checkpoint_artifact(artifact, checkpoint["artifact_sha256"])
                if stage != checkpoint["stage_id"]:
                    raise ValueError("Checkpoint stage mismatch")
                if stage == "stores.write" and (
                    record.get("stores_write_status") != "SUCCEEDED"
                    or checkpoint_payload.get("payload_kind") != "report_inputs"
                ):
                    raise ValueError("Report-only checkpoint is incomplete")
            except (OSError, ValueError, json.JSONDecodeError):
                details["resume_available"] = False
                details["resume_mode"] = None
            else:
                stage_order = IngestionRunRepository.STAGES
                checkpoint_index = stage_order.index(checkpoint["stage_id"])
                details["checkpoint_stage"] = checkpoint["stage_id"]
                details["resume_from_stage"] = stage_order[checkpoint_index + 1]
        progress = read_progress_file(job_id) or {}
        for field in (
            "progress_pct", "files_done", "files_total", "blocks_extracted",
            "entities_found", "relationships_found", "weaviate_chunks",
            "neo4j_nodes", "warning_count", "phase",
        ):
            if field in progress:
                details[field] = progress[field]
        details["ray_dashboard_url"] = f"{RAY_DASHBOARD_PUBLIC_URL}/#/jobs/{job_id}"
        return details
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get job status: {str(e)}")


@app.post("/jobs/{job_id}/resume", status_code=202, tags=["Ingestion Jobs"])
async def resume_ingestion_job(
    job_id: str,
    body: ResumeJobRequest | None = None,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
    actor: AuthenticatedActor = Depends(require_authenticated_actor),
):
    """Resume a failed/cancelled run from its latest durable stage checkpoint."""
    repository = IngestionRunRepository()
    record = await asyncio.to_thread(repository.get_resume_record, job_id)
    if record is None or (
        record["submitted_by"] != actor.user_id
        and "ingestion:jobs:resume_any" not in actor.permissions
    ):
        raise HTTPException(status_code=404, detail="Job not found")
    prior_attempt = await asyncio.to_thread(
        repository.get_resume_attempt, record["run_id"], idempotency_key
    )
    if prior_attempt is not None:
        latest_checkpoint = await asyncio.to_thread(repository.get_latest_checkpoint, record["run_id"])
        prior_mode = (
            "REPORT_ONLY"
            if record.get("stores_write_status") == "SUCCEEDED"
            and latest_checkpoint and latest_checkpoint.get("stage_id") == "stores.write"
            else "FROM_CHECKPOINT"
        )
        return {
            "job_id": job_id, "run_id": record["run_id"],
            "attempt": prior_attempt["attempt"], "status": prior_attempt["status"],
            "restart_mode": prior_mode,
            "ray_submission_id": prior_attempt["ray_submission_id"],
            "message": "This resume request was already accepted.",
        }
    if record["status"] not in {"FAILED", "CANCELLED"}:
        raise HTTPException(status_code=409, detail="Only failed or cancelled jobs can be resumed")
    source_manifest = record.get("source_manifest_json") or {}
    source = await asyncio.to_thread(_verify_resume_source_manifest, source_manifest)
    if source is None:
        raise HTTPException(
            status_code=409,
            detail="The original input files are missing or changed. Submit a new ingestion run.",
        )
    run_context = record.get("run_context_json") or {}
    if not isinstance(run_context, dict) or not run_context.get("customer_ontology_id"):
        raise HTTPException(status_code=409, detail="The frozen ingestion context is unavailable for resume.")
    for field in (
        "customer_id", "tenant_id", "industry_id", "domain_id", "product_line_id",
        "customer_ontology_id", "customer_ontology_version", "canonical_ontology_id",
        "canonical_ontology_version", "extraction_policy_version",
    ):
        if run_context.get(field) != record.get(field):
            raise HTTPException(
                status_code=409,
                detail="The stored frozen context is incomplete or inconsistent. Submit a new run.",
            )
    if record["force_rewrite_requested"] and actor.auth_source != "internal":
        _require_force_rewrite_permission(actor)

    checkpoint = await asyncio.to_thread(repository.get_latest_checkpoint, record["run_id"])
    if not checkpoint or checkpoint.get("stage_id") not in {
        "blocks.prepared", "lane2.pass2", "admission.plan", "stores.write",
    }:
        raise HTTPException(status_code=409, detail="No supported durable stage checkpoint is available.")
    report_only_recovery = (
        checkpoint.get("stage_id") == "stores.write"
        and record.get("stores_write_status") == "SUCCEEDED"
    )
    if record.get("stores_write_status") == "SUCCEEDED" and not report_only_recovery:
        raise HTTPException(
            status_code=409,
            detail="Stores succeeded, but no verified report-input checkpoint is available.",
        )
    if record.get("stores_write_status") not in {"PENDING", "SUCCEEDED"} and checkpoint.get("stage_id") != "admission.plan":
        raise HTTPException(
            status_code=409,
            detail="Partial store publishing requires the verified admission-plan checkpoint.",
        )
    from kbfabric_ingest.ingest.checkpoints import read_checkpoint_artifact, stable_digest
    manifest_digest = stable_digest(source_manifest)
    context_digest = stable_digest(run_context)
    if (checkpoint.get("source_manifest_sha256") != manifest_digest
            or checkpoint.get("context_sha256") != context_digest):
        raise HTTPException(status_code=409, detail="Checkpoint source or frozen context has changed.")
    persist_dir = (os.environ.get("KBFABRIC_INGEST_PERSIST_DIR") or "").strip() or str(BASE_DIR)
    checkpoint_path = Path(checkpoint["artifact_uri"]).resolve()
    checkpoint_root = (Path(persist_dir) / "checkpoints" / record["run_id"]).resolve()
    if not checkpoint_path.is_relative_to(checkpoint_root):
        raise HTTPException(status_code=409, detail="Checkpoint artifact is outside the run checkpoint directory.")
    try:
        artifact_stage, artifact_payload = read_checkpoint_artifact(checkpoint_path, checkpoint["artifact_sha256"])
        if artifact_stage != checkpoint["stage_id"]:
            raise ValueError("Checkpoint stage mismatch")
        if artifact_stage == "stores.write" and artifact_payload.get("payload_kind") != "report_inputs":
            raise ValueError("Report-only checkpoint payload is missing")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail=f"Checkpoint artifact is unavailable or invalid: {exc}") from exc

    config = get_app_config()
    client = get_job_client()
    try:
        ray_jobs = await asyncio.to_thread(client.list_jobs)
        running_jobs = [j for j in ray_jobs if j.status in [JobStatus.PENDING, JobStatus.RUNNING]]
        if len(running_jobs) >= config["scheduler"]["max_concurrent_jobs"]:
            raise HTTPException(status_code=429, detail="Ingestion capacity is full; retry resume later.")
        is_locked, active_job_id = await asyncio.to_thread(
            check_domain_lock, record["domain_id"], record["customer_id"]
        )
        if is_locked:
            raise HTTPException(
                status_code=409,
                detail=f"Domain already has an active ingestion job ({active_job_id}).",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not verify Ray capacity: {exc}") from exc

    try:
        claim = await asyncio.to_thread(
            repository.claim_resume,
            record["run_id"], idempotency_key=idempotency_key,
            requested_by=actor.user_id, reason=(body.reason if body else None),
            automatic_retry=actor.auth_source == "internal",
        )
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # A repeated idempotency key returns the same attempt; do not dispatch it twice.
    if claim["already_claimed"]:
        return {
            "job_id": job_id, "run_id": record["run_id"], "attempt": claim["attempt"],
            "status": claim["status"],
            "restart_mode": "REPORT_ONLY" if claim.get("reconciliation_mode") == "REPORT_ONLY" else "FROM_CHECKPOINT",
            "resume_from_stage": claim["resume_from_stage"],
            "message": "This resume request was already accepted.",
        }

    persist_dir = (os.environ.get("KBFABRIC_INGEST_PERSIST_DIR") or "").strip() or str(BASE_DIR)
    ray_dir = _resolve_ray_worker_dir(persist_dir)
    worker_input_dir = source_manifest.get("worker_input_dir") or source
    python_path = _get_ray_python_path(persist_dir)
    script_path = f"{ray_dir.rstrip('/')}/scripts/run_ingestion_job.py"
    entrypoint_parts = [
        python_path, script_path, "--job-id", job_id, "--run-id", record["run_id"],
        "--submitted-by", record["submitted_by"],
        "--auth-source", record["auth_source"], "--customer-id", record["customer_id"],
        "--customer-ontology-version", str(record["customer_ontology_version"]),
        "--domain", record["domain_id"], "--product-line-id", record["product_line_id"],
        "--document-nature", record["document_nature"], "--input-dir", worker_input_dir,
    ]
    if record["hitl_mode"]:
        entrypoint_parts.extend(["--hitl-mode", record["hitl_mode"]])
    if record["force_rewrite_requested"]:
        entrypoint_parts.append("--force-rewrite")
    audit_context = _attach_submission_metadata({
        **run_context, "run_id": record["run_id"], "job_id": job_id,
        "ingestion_batch_id": record["ingestion_batch_id"],
        "submitted_by": record["submitted_by"], "auth_source": record["auth_source"],
        "document_nature": record["document_nature"], "hitl_mode": record["hitl_mode"],
        "force_rewrite_requested": record["force_rewrite_requested"],
        "execution_attempt": claim["attempt"], "resume_requested_by": actor.user_id,
        "resume_from_stage": claim["resume_from_stage"],
        "reconciliation_mode": claim.get("reconciliation_mode"),
    }, record.get("submission_metadata_json") or {})
    try:
        await asyncio.to_thread(
            client.submit_job,
            entrypoint=shlex.join(str(part) for part in entrypoint_parts),
            submission_id=claim["ray_submission_id"],
            runtime_env=_get_ray_runtime_env(persist_dir, config, audit_context=audit_context),
        )
    except Exception as exc:
        await asyncio.to_thread(
            repository.update_status, record["run_id"], "FAILED",
            error_message=f"Resume dispatch failed: {exc}",
        )
        raise HTTPException(status_code=503, detail="Ray could not accept the resume attempt.") from exc

    return {
        "job_id": job_id, "run_id": record["run_id"], "attempt": claim["attempt"],
        "status": "QUEUED",
        "restart_mode": "REPORT_ONLY" if claim.get("reconciliation_mode") == "REPORT_ONLY" else "FROM_CHECKPOINT",
        "resume_from_stage": claim["resume_from_stage"],
        "checkpoint_stage": claim["checkpoint_stage"],
        "reconciliation_mode": claim["reconciliation_mode"],
        "ray_submission_id": claim["ray_submission_id"],
        "message": (
            f"Resume accepted at {claim['resume_from_stage']} using the {claim['checkpoint_stage']} checkpoint. "
            + ("Both stores will be replayed with idempotent upserts." if claim["reconciliation_mode"] == "IDEMPOTENT_STORE_REPLAY" else "")
        ),
    }


@app.get("/jobs/{job_id}/events")
async def stream_job_events(
    job_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    actor: AuthenticatedActor = Depends(require_authenticated_actor),
):
    """Replay durable run events, then poll for newly committed events."""
    repository = IngestionRunRepository()
    details = await asyncio.to_thread(repository.get_job_details, job_id)
    if details is None or (
        details["submitted_by"] != actor.user_id
        and "ingestion:jobs:read_all" not in actor.permissions
    ):
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        cursor = await asyncio.to_thread(repository.resolve_event_cursor, job_id, last_event_id) if last_event_id else 0
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def event_frames():
        nonlocal cursor
        heartbeat_at = time.monotonic()
        while not await request.is_disconnected():
            events = await asyncio.to_thread(repository.list_events_after, job_id, cursor, 100)
            for event in events:
                cursor = int(event["event_sequence"])
                yield (
                    f"id: {event['event_id']}\n"
                    f"event: {event['event_type']}\n"
                    f"data: {json.dumps(event, default=str, separators=(',', ':'))}\n\n"
                )
            if not events and time.monotonic() - heartbeat_at >= 15:
                yield ": keep-alive\n\n"
                heartbeat_at = time.monotonic()
            await asyncio.sleep(2)

    return StreamingResponse(
        event_frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/jobs/{job_id}/logs")
async def get_job_logs(job_id: str):
    """Get job logs from Ray."""
    try:
        client = get_job_client()
        logs = client.get_job_logs(job_id)
        return {"job_id": job_id, "logs": logs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get logs: {str(e)}")


@app.post("/jobs/{job_id}/cancel", status_code=202, tags=["Ingestion Jobs"])
async def cancel_ingestion_job(
    job_id: str,
    actor: AuthenticatedActor = Depends(require_authenticated_actor),
):
    """Request cooperative cancellation before the worker enters store publishing."""
    repository = IngestionRunRepository()
    record = await asyncio.to_thread(repository.get_resume_record, job_id)
    if record is None or (
        record["submitted_by"] != actor.user_id
        and "ingestion:jobs:cancel_any" not in actor.permissions
    ):
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        result = await asyncio.to_thread(
            repository.request_cancel, job_id, requested_by=actor.user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "job_id": job_id, "run_id": result["run_id"], "status": result["status"],
        "cancel_requested": True,
        "message": "Cancellation will be applied at the next safe stage boundary.",
    }


@app.delete("/jobs/{job_id}")
async def stop_job(job_id: str):
    """Stop a running job."""
    try:
        client = get_job_client()
        success = client.stop_job(job_id)
        return {"job_id": job_id, "stopped": success}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop job: {str(e)}")


@app.get("/progress/{job_id}")
async def get_progress(job_id: str):
    """Get job progress from file (fast, no Ray call)."""
    progress = read_progress_file(job_id)
    if progress:
        return progress
    raise HTTPException(status_code=404, detail=f"No progress file for job: {job_id}")


def _customer_ingestion_context(customer_id: str) -> dict[str, Any]:
    """Return configured ontology choices for the explicitly selected active customer."""
    from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository
    from kbfabric_ingest.fabric_foundry.knowledge_contextualization.repository import CustomerOntologyRepository
    from kbfabric_ingest.fabric_foundry.runtime.effective_ontology_context import compile_customer_runtime_ontology

    customer = CustomerRepository().get_by_customer_id(customer_id=customer_id)
    if customer is None:
        raise ValueError(f"No ACTIVE customer found: {customer_id}")
    ontology = CustomerOntologyRepository().get_active(customer.customer_id)
    if ontology is None:
        raise ValueError(f"Active Customer Ontology not found for {customer.customer_id}")

    definition = ontology.definition
    source_domains = definition.get("domains", [])
    domains: list[dict[str, Any]] = []
    for source_domain in source_domains if isinstance(source_domains, list) else []:
        if not isinstance(source_domain, dict):
            continue
        domain_id = source_domain.get("name")
        if not isinstance(domain_id, str) or not domain_id:
            continue
        customer_context = source_domain.get("customer_context") or {}
        if not isinstance(customer_context, dict):
            continue
        compiled = compile_customer_runtime_ontology(definition, domain_id=domain_id)
        runtime_domains = compiled.get("domains", [])
        runtime_domain = next(
            (item for item in runtime_domains if isinstance(item, dict) and item.get("name") == domain_id),
            None,
        )
        if runtime_domain is None:
            continue

        product_lines: list[dict[str, str]] = []
        canonical_spine = source_domain.get("spine", [])
        configured_spine = ((customer_context.get("spine") or {}).get("product_lines") or {})
        for canonical_pl in canonical_spine if isinstance(canonical_spine, list) else []:
            if not isinstance(canonical_pl, dict):
                continue
            canonical_name = canonical_pl.get("ProductLine")
            if not isinstance(canonical_name, str):
                continue
            state = configured_spine.get(canonical_name, {}) if isinstance(configured_spine, dict) else {}
            if not isinstance(state, dict) or state.get("applicable") is not True:
                continue
            aliases = state.get("aliases") or []
            runtime_name = aliases[0] if aliases else canonical_name
            runtime_pl = next(
                (pl for pl in runtime_domain.get("spine", []) if isinstance(pl, dict)
                 and pl.get("ProductLine") == runtime_name),
                None,
            )
            if runtime_pl is not None:
                product_lines.append({
                    "id": f"DOMAIN:{domain_id}|PRODUCT_LINE:{canonical_name}",
                    "name": runtime_pl["ProductLine"],
                })

        additions = customer_context.get("additions") or {}
        added_product_lines = additions.get("product_lines", []) if isinstance(additions, dict) else []
        for added_pl in added_product_lines if isinstance(added_product_lines, list) else []:
            if not isinstance(added_pl, dict) or added_pl.get("applicable") is not True:
                continue
            aliases = added_pl.get("aliases") or []
            product_lines.append({
                "id": str(added_pl.get("id")),
                "name": str(aliases[0] if aliases else added_pl.get("name")),
            })

        if product_lines:
            domain_aliases = customer_context.get("aliases") or []
            domains.append({
                "id": domain_id,
                "name": domain_aliases[0] if domain_aliases else source_domain.get("display_name", domain_id),
                "product_lines": product_lines,
            })

    return {
        "customer_id": customer.customer_id,
        "customer_name": customer.customer_name,
        "tenant_id": str(customer.tenant_id),
        "industry_id": customer.industry_id,
        "customer_ontology_id": str(ontology.customer_ontology_id),
        "customer_ontology_version": ontology.customer_ontology_version,
        "domains": domains,
    }


@app.get(
    "/ingestion/customers",
    response_model=IngestionCustomersResponse,
    tags=["Ingestion Configuration"],
    summary="List active customers selectable for ingestion",
    description=(
        "Returns active customer workspaces for the interim Ingestion Studio selector. "
        "This is context selection only; customer-level authorization is not yet implemented."
    ),
)
async def list_ingestion_customers(
    actor: AuthenticatedActor = Depends(require_authenticated_actor),
):
    from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository

    customers = CustomerRepository().list_active_customers()
    return {
        "customers": [
            {
                "customer_id": item.customer_id,
                "customer_name": item.customer_name,
                "industry_id": item.industry_id,
            }
            for item in customers
        ]
    }


@app.get(
    "/ingestion/context",
    response_model=IngestionContextResponse,
    tags=["Ingestion Configuration"],
    summary="Get configured domains and product lines for ingestion",
    description=(
        "Ingestion Studio bootstrap endpoint. Resolves the explicitly selected ACTIVE "
        "customer and returns only domains and product lines marked applicable in its "
        "active persisted Customer Ontology. The UI selects a domain first, then a product "
        "line from that domain. Industry YAML is used to build the ontology but is not a "
        "runtime selection fallback. Until customer-level authorization is integrated, "
        "customer_id selects run context but does not establish user entitlement."
    ),
    responses={409: {"description": "Selected customer has no active ontology."}, 422: {"description": "Selected customer is not active or does not exist."}},
)
async def get_ingestion_context(
    customer_id: str = Query(..., min_length=1, description="Active customer selected for this ingestion workspace"),
    actor: AuthenticatedActor = Depends(require_authenticated_actor),
) -> IngestionContextResponse:
    try:
        return _customer_ingestion_context(customer_id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(
    "/domains",
    tags=["Ingestion Configuration"],
    summary="List domains configured for a selected customer",
    description="Customer-filtered domain catalog with configured nested product_lines. Supply customer_id; for the complete bootstrap context, call GET /ingestion/context?customer_id=... .",
)
async def list_domains(
    customer_id: str = Query(..., min_length=1),
    actor: AuthenticatedActor = Depends(require_authenticated_actor),
):
    try:
        context = _customer_ingestion_context(customer_id)
        return {
            "customer_id": context["customer_id"],
            "domains": [
                {
                    "id": item["id"],
                    "name": item["id"],
                    "display_name": item["name"],
                    "description": "",
                    "product_lines": item["product_lines"],
                }
                for item in context["domains"]
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _validate_ingestion_selection(
    customer_id: str, domain_id: str, product_line_id: str
) -> dict[str, Any]:
    """Resolve and freeze the explicitly selected customer's ontology for this run."""
    from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository
    from kbfabric_ingest.fabric_foundry.runtime.effective_ontology_context import EffectiveDomainOntologyResolver

    customer = CustomerRepository().get_by_customer_id(customer_id=customer_id)
    if customer is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "ACTIVE_CUSTOMER_NOT_FOUND", "customer_id": customer_id},
        )
    try:
        context = EffectiveDomainOntologyResolver().resolve(
            customer_id=customer.customer_id,
            domain_id=domain_id,
            product_line_id=product_line_id,
        )
        policy = context.extraction_policy or {}
        return {
            "customer_id": context.customer_id,
            "tenant_id": context.tenant_id,
            "industry_id": customer.industry_id,
            "domain_id": context.domain_id,
            "product_line_id": context.product_line_id or product_line_id,
            "customer_ontology_id": context.customer_ontology_id,
            "customer_ontology_version": context.customer_ontology_version,
            "canonical_ontology_id": context.ontology_id,
            "canonical_ontology_version": context.ontology_version,
            "extraction_policy_version": str(policy.get("version") or policy.get("policy_version") or ""),
        }
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INGESTION_CONTEXT_SELECTION_INVALID",
                "message": str(exc),
                "customer_id": customer.customer_id,
                "domain_id": domain_id,
                "product_line_id": product_line_id,
            },
        ) from exc


def _attach_submission_metadata(
    audit_context: dict[str, Any], submission_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep caller-supplied provenance attached to the Ray worker context."""
    return {**audit_context, "submission_metadata": submission_metadata or {}}


def _ingestion_run_record(
    *, run_id: str, job_id: str, actor: AuthenticatedActor,
    frozen_context: dict[str, Any], domain_id: str, product_line_id: str,
    document_nature: str, hitl_mode: str | None, force_rewrite_requested: bool,
    submission_metadata: dict[str, Any] | None = None,
    source_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build audit evidence from trusted auth and the resolved ontology context."""
    return {
        "run_id": run_id,
        "job_id": job_id,
        # IngestionService currently uses the Ray job ID as its batch ID.
        "ingestion_batch_id": job_id,
        "submitted_by": actor.user_id,
        "auth_source": actor.auth_source,
        **frozen_context,
        "document_nature": document_nature,
        "hitl_mode": hitl_mode,
        "force_rewrite_requested": force_rewrite_requested,
        "run_context": frozen_context,
        "submission_metadata": submission_metadata or {},
        "source_manifest": source_manifest or {},
    }


def _build_directory_source_manifest(input_path: Path, worker_input_dir: str) -> dict[str, Any]:
    """Capture a stable inventory of supported source files without storing their content."""
    supported = {".docx", ".pdf", ".xlsx", ".txt", ".csv", ".pptx"}
    files: list[dict[str, Any]] = []
    inventory_complete = input_path.is_dir()
    if inventory_complete:
        for path in sorted(input_path.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in supported:
                continue
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            files.append({
                "name": path.relative_to(input_path).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
            })
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "kind": "directory",
        "input_dir": str(input_path),
        "worker_input_dir": worker_input_dir,
        "files": files,
        "inventory_complete": inventory_complete,
        "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _verify_resume_source_manifest(manifest: dict[str, Any]) -> str | None:
    """Return the worker-visible source path only when all original files still match."""
    if not isinstance(manifest, dict) or not manifest.get("inventory_complete"):
        return None
    input_dir = Path(str(manifest.get("input_dir") or "")).expanduser()
    worker_input_dir = str(manifest.get("worker_input_dir") or "").strip()
    expected_files = manifest.get("files")
    if not input_dir.is_dir() or not worker_input_dir or not isinstance(expected_files, list) or not expected_files:
        return None

    if manifest.get("kind") == "directory":
        current = _build_directory_source_manifest(input_dir, worker_input_dir)
        if current.get("manifest_sha256") != manifest.get("manifest_sha256"):
            return None
        return worker_input_dir

    if manifest.get("kind") != "upload":
        return None
    actual_files = []
    for item in expected_files:
        name = item.get("name") if isinstance(item, dict) else None
        if not name:
            return None
        path = (input_dir / name).resolve()
        if not path.is_relative_to(input_dir.resolve()) or not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as source_file:
            for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_files.append({
            "name": name, "size_bytes": path.stat().st_size, "sha256": digest.hexdigest(),
        })
    canonical = json.dumps(actual_files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != manifest.get("manifest_sha256"):
        return None
    return worker_input_dir


@app.get("/reports")
async def list_reports():
    """List available HTML reports."""
    reports = []
    if REPORTS_DIR.exists():
        # Include nested reports (e.g. reports/semantic_ingestion/*.html)
        for f in REPORTS_DIR.rglob("*.html"):
            rel = str(f.relative_to(REPORTS_DIR)).replace("\\", "/")
            reports.append({
                "name": rel,
                "url": f"/reports/{rel}",
                "size_kb": round(f.stat().st_size / 1024, 1),
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()
            })
    return {"reports": sorted(reports, key=lambda x: x["modified"], reverse=True)}


@app.get("/reports/get-report/{job_id}")
async def get_report_by_job(job_id: str):
    """Download unified report by job id."""
    if "/" in job_id or "\\" in job_id:
        raise HTTPException(status_code=400, detail="Invalid job id")
    report_base = job_id if job_id.startswith("ingest-") else f"ingest-{job_id}"
    report_name = f"{report_base}.html"
    report_file = REPORTS_DIR / report_name
    if not report_file.is_file():
        raise HTTPException(status_code=404, detail=f"Report not found for job: {job_id}")
    # The pipeline report is first generated at ingestion completion. Refresh
    # its audit sections on download so later HITL decisions and write attempts
    # appear at the unchanged report URL.
    try:
        run_details = await asyncio.to_thread(
            IngestionRunRepository().get_job_details, report_base,
        )
        if run_details:
            batch_id = run_details.get("ingestion_batch_id")
            domain_id = run_details.get("domain_id")
            product_line_id = run_details.get("product_line_id")
            if batch_id and domain_id and product_line_id:
                audit_events = await asyncio.to_thread(
                    HITLReviewRepository().list_report_audit,
                    ingestion_batch_id=str(batch_id),
                    domain_id=str(domain_id),
                    product_line_id=str(product_line_id),
                )
                refreshed = await asyncio.to_thread(
                    UnifiedIngestionReportGenerator().refresh_audit_sections,
                    report_file, run_details, audit_events,
                    datetime.now(timezone.utc).isoformat(),
                )
                if not refreshed:
                    logger.warning("Report audit refresh skipped; report lacks audit markers", job_id=report_base)
    except Exception as exc:
        # Continue serving the last generated report if the audit store is
        # unavailable; never turn an otherwise available report into a 500.
        logger.warning("Could not refresh report HITL audit snapshot", job_id=report_base, error=str(exc))
    return FileResponse(
        report_file,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{report_name}"'},
    )


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    content, content_type = get_metrics_response()
    return Response(content=content, media_type=content_type)


@app.get("/config")
async def get_config():
    """
    Get safe configuration values for display in UI.
    
    Returns non-sensitive configuration values that are useful for troubleshooting
    and understanding system behavior. Excludes passwords, API keys, and other secrets.
    """
    try:
        config = get_app_config()

        ollama_cfg = config["ollama"]
        ingestion_cfg = config["ingestion"]
        embeddings_cfg = config["embeddings"]

        embeddings_dir = (
            (os.environ.get("KBFABRIC_EMBEDDINGS_MODEL_DIR") or "").strip()
            or embeddings_cfg["local_model_path"]
        )

        return {
            "ollama": {
                "base_url": ollama_cfg["base_url"],
                "text_model": ollama_cfg["text_model"],
                "vision_model": ollama_cfg["vision_model"],
                "timeout_sec": ollama_cfg["timeout_sec"],
            },
            "ingestion": {
                "max_chars_per_block": ingestion_cfg["max_chars_per_block"],
            },
            "ollama_text": {
                "batch_size": ollama_cfg["text"]["batch_size"],
                "batch_delay_seconds": ollama_cfg["text"]["batch_delay_seconds"],
            },
            "ollama_vision": {
                "batch_size": ollama_cfg["vision"]["batch_size"],
                "batch_delay_seconds": ollama_cfg["vision"]["batch_delay_seconds"],
            },
            "embeddings": {
                "model_name": embeddings_cfg["model_name"],
                "local_model_path": embeddings_cfg["local_model_path"],
                "effective_model_dir": embeddings_dir,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load configuration: {e}") from e


@app.get("/")
async def root():
    """API info."""
    return {
        "name": "Pulse Knowledge Fabric - Ingestion API",
        "version": "2.0.0",
        "docs": "/docs",
        "ray_dashboard": RAY_DASHBOARD_PUBLIC_URL,
        "endpoints": {
            "submit_job": "POST /jobs",
            "list_jobs": "GET /jobs",
            "job_status": "GET /jobs/{job_id}",
            "job_logs": "GET /jobs/{job_id}/logs",
            "job_progress": "GET /progress/{job_id}",
            "stop_job": "DELETE /jobs/{job_id}",
            "domains": "GET /domains",
            "reports": "GET /reports",
            "get_report": "GET /reports/get-report/{job_id}",
            "health": "GET /health"
        }
    }


if __name__ == "__main__":
    import uvicorn
    _host = require_env("KBFABRIC_API_HOST")
    _port = int(require_env("KBFABRIC_API_PORT"))
    uvicorn.run(app, host=_host, port=_port)

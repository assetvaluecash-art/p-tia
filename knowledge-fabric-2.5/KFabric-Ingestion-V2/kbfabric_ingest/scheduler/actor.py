"""Ray Actor for document ingestion with job management.

This actor wraps the IngestionService and provides:
- Job tracking and history
- Async execution via Ray
- Notification callbacks

Usage:
    import ray
    from kbfabric_ingest.scheduler.actor import IngestionActor
    
    ray.init()
    actor = IngestionActor.remote()
    
    # Submit job
    result = ray.get(actor.submit_job.remote("/path/to/docs", "wealth_portfolio"))
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import ray

from kbfabric_ingest.scheduler.models import JobStatus, JobResult, JobSummary
from kbfabric_ingest.scheduler.progress import ProgressTracker


@ray.remote
class IngestionActor:
    """Ray Actor for managing ingestion jobs.
    
    This actor:
    1. Maintains job history in memory
    2. Runs ingestion synchronously (Ray handles parallelism)
    3. Provides job status queries
    """
    
    def __init__(self):
        """Initialize the ingestion actor."""
        self._service = None
        self._job_history: list[JobResult] = []
        self._current_job: Optional[str] = None
        self._current_job_info: dict = {}  # Store pending job info for status queries
        
        # Lazy import to avoid import issues at actor creation
        self._init_service()
    
    def _init_service(self):
        """Initialize the ingestion service (lazy loading)."""
        if self._service is not None:
            return
        
        try:
            from kbfabric_ingest.ingest.ingestion_service import IngestionService
            print("[IngestionActor] Initializing IngestionService...", flush=True)
            self._service = IngestionService()
            print("[IngestionActor] IngestionService initialized", flush=True)
        except Exception as e:
            print(f"[IngestionActor] ERROR: Failed to initialize IngestionService: {e}", flush=True)
            raise
    
    def submit_job(
        self,
        input_dir: str,
        domain: str,
        job_id: Optional[str] = None,
        start_delay_minutes: int = 0,
        force_rewrite: bool = False
    ) -> dict[str, Any]:
        """
        Submit and execute an ingestion job.
        
        Args:
            input_dir: Path to directory containing documents
            domain: Domain name (e.g., "wealth_portfolio")
            job_id: Optional job ID (generated if not provided)
            start_delay_minutes: Delay before starting (0 = immediate)
            force_rewrite: Unsupported here; use the authenticated customer-scoped jobs API
            
        Returns:
            JobResult as dictionary
        """
        import time
        
        if force_rewrite:
            raise ValueError(
                "Force rewrite is unavailable through the legacy actor because it "
                "does not carry customer, product-line, and permission scope. "
                "Use the authenticated /jobs API."
            )

        # Generate job ID if not provided
        if not job_id:
            job_id = f"job-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        
        self._current_job = job_id
        
        # Store job info immediately for status queries
        self._current_job_info = {
            "job_id": job_id,
            "domain": domain,
            "input_dir": input_dir,
            "scheduled_time": datetime.now(timezone.utc).isoformat(),
            "start_delay_minutes": start_delay_minutes,
            "force_rewrite": force_rewrite,
            "status": "scheduled" if start_delay_minutes > 0 else "running"
        }
        
        # Handle delayed start
        if start_delay_minutes > 0:
            print(f"[IngestionActor][{job_id}] Job scheduled to start in {start_delay_minutes} minute(s)...", flush=True)
            total_seconds = start_delay_minutes * 60
            for remaining in range(total_seconds, 0, -10):
                mins = remaining // 60
                secs = remaining % 60
                print(f"[IngestionActor][{job_id}] Countdown: {mins}m {secs}s remaining...", flush=True)
                self._current_job_info["countdown_sec"] = remaining
                time.sleep(10)
            print(f"[IngestionActor][{job_id}] Countdown complete, starting ingestion!", flush=True)
        
        start_time = datetime.now(timezone.utc)
        self._current_job_info["start_time"] = start_time.isoformat()
        self._current_job_info["status"] = "running"
        
        print(f"[IngestionActor][{job_id}] Starting job: domain={domain}, input_dir={input_dir}", flush=True)
        
        # Create initial job result
        result = JobResult(
            job_id=job_id,
            status=JobStatus.RUNNING,
            domain=domain,
            input_dir=input_dir,
            start_time=start_time
        )
        
        try:
            # Ensure service is initialized
            self._init_service()
            
            # Create progress tracker (for status updates, though service doesn't use it directly)
            progress_tracker = ProgressTracker(job_id, domain, input_dir)
            progress_tracker.start()
            
            # Run ingestion
            ingest_result = self._service.run_ingestion(Path(input_dir), domain)
            
            # Mark progress complete with report paths
            progress_tracker.complete(ingest_result.get("report_paths", {}))
            
            end_time = datetime.now(timezone.utc)
            duration = (end_time - start_time).total_seconds()
            
            # Check for errors
            if ingest_result.get("error"):
                result.status = JobStatus.FAILED
                result.error = ingest_result.get("error")
            else:
                result.status = JobStatus.COMPLETED
            
            # Populate metrics
            result.end_time = end_time
            result.duration_sec = duration
            result.total_files = ingest_result.get("total_files", 0)
            result.total_blocks = ingest_result.get("total_blocks", 0)
            result.entities_extracted = ingest_result.get("entities_extracted", 0)
            result.weaviate_chunks = ingest_result.get("weaviate_chunks", 0)
            result.neo4j_nodes = ingest_result.get("neo4j_nodes", 0)
            result.neo4j_relationships = ingest_result.get("neo4j_relationships", 0)
            result.report_paths = ingest_result.get("report_paths", {})
            result.errors = ingest_result.get("errors", [])
            
            print(f"[IngestionActor][{job_id}] Job completed: status={result.status.value}, blocks={result.total_blocks}", flush=True)
            
        except Exception as e:
            end_time = datetime.now(timezone.utc)
            result.status = JobStatus.FAILED
            result.end_time = end_time
            result.duration_sec = (end_time - start_time).total_seconds()
            result.error = str(e)
            
            print(f"[IngestionActor][{job_id}] Job FAILED: {e}", flush=True)
        
        # Store in history
        self._job_history.append(result)
        self._current_job = None
        self._current_job_info = {}
        
        return result.to_dict()
    
    def get_job_status(self, job_id: str) -> dict[str, Any]:
        """Get status of a specific job."""
        # Check if it's the current running job
        if self._current_job == job_id:
            info = self._current_job_info.copy()
            info["status"] = JobStatus.RUNNING.value
            info["message"] = "Job is currently running"
            # Calculate elapsed time
            if "start_time" in info:
                from datetime import datetime, timezone
                start = datetime.fromisoformat(info["start_time"].replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - start).total_seconds()
                info["elapsed_sec"] = round(elapsed, 1)
            return info
        
        # Search history
        for job in self._job_history:
            if job.job_id == job_id:
                return job.to_dict()
        
        return {
            "job_id": job_id,
            "status": "not_found",
            "message": f"Job {job_id} not found"
        }
    
    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        """List recent jobs."""
        jobs = []
        
        # Add current job if running
        if self._current_job:
            info = self._current_job_info.copy()
            info["status"] = JobStatus.RUNNING.value
            info["message"] = "Currently running"
            jobs.append(info)
        
        # Add history (most recent first)
        for job in reversed(self._job_history[-limit:]):
            jobs.append({
                "job_id": job.job_id,
                "status": job.status.value,
                "domain": job.domain,
                "input_dir": job.input_dir,
                "start_time": job.start_time.isoformat() if job.start_time else None,
                "end_time": job.end_time.isoformat() if job.end_time else None,
                "total_blocks": job.total_blocks,
                "error": job.error
            })
        
        return jobs
    
    def get_job_reports(self, job_id: str) -> dict[str, Any]:
        """Get report paths for a completed job."""
        for job in self._job_history:
            if job.job_id == job_id:
                if job.status == JobStatus.COMPLETED:
                    return {
                        "job_id": job_id,
                        "status": "completed",
                        "report_paths": job.report_paths
                    }
                else:
                    return {
                        "job_id": job_id,
                        "status": job.status.value,
                        "error": "Reports only available for completed jobs"
                    }
        
        return {
            "job_id": job_id,
            "status": "not_found",
            "error": f"Job {job_id} not found"
        }
    
    def cancel_job(self, job_id: str) -> dict[str, Any]:
        """Cancel a pending or running job (best effort)."""
        # Note: Ray doesn't support true cancellation of running tasks
        # This is a placeholder for future implementation
        if self._current_job == job_id:
            return {
                "job_id": job_id,
                "status": "warning",
                "message": "Cannot cancel running job in current Ray version"
            }
        
        return {
            "job_id": job_id,
            "status": "not_found",
            "message": f"Job {job_id} not found or already completed"
        }
    
    def health_check(self) -> dict[str, Any]:
        """Check actor health."""
        return {
            "status": "healthy",
            "service_initialized": self._service is not None,
            "current_job": self._current_job,
            "jobs_completed": len(self._job_history)
        }


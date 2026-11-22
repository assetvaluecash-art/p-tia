"""Data models for job scheduling."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class JobStatus(str, Enum):
    """Job execution status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobRequest:
    """Request to schedule an ingestion job."""
    input_dir: str
    domain: str
    scheduled_time: Optional[datetime] = None
    notify_email: Optional[str] = None
    notify_teams: bool = False


@dataclass
class JobResult:
    """Result of a completed ingestion job."""
    job_id: str
    status: JobStatus
    domain: str
    input_dir: str
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_sec: Optional[float] = None
    # Ingestion metrics
    total_files: int = 0
    total_blocks: int = 0
    entities_extracted: int = 0
    weaviate_chunks: int = 0
    neo4j_nodes: int = 0
    neo4j_relationships: int = 0
    # Reports
    report_paths: dict[str, str] = field(default_factory=dict)
    # Error info
    error: Optional[str] = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "domain": self.domain,
            "input_dir": self.input_dir,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_sec": self.duration_sec,
            "total_files": self.total_files,
            "total_blocks": self.total_blocks,
            "entities_extracted": self.entities_extracted,
            "weaviate_chunks": self.weaviate_chunks,
            "neo4j_nodes": self.neo4j_nodes,
            "neo4j_relationships": self.neo4j_relationships,
            "report_paths": self.report_paths,
            "error": self.error,
            "errors": self.errors
        }


@dataclass
class JobSummary:
    """Summary of a job for listing."""
    job_id: str
    status: JobStatus
    domain: str
    input_dir: str
    start_time: datetime
    end_time: Optional[datetime] = None
    total_blocks: int = 0
    error: Optional[str] = None


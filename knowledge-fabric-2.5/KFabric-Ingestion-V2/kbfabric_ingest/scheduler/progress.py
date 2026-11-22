"""File-based progress tracking for ingestion jobs.

This module provides real-time progress updates that can be polled by the UI
without blocking on the Ray actor.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from kbfabric_ingest._common.config import BASE_DIR

# Progress file location
PROGRESS_FILE = BASE_DIR / "current_job_progress.json"


class ProgressTracker:
    """Tracks and persists job progress to a JSON file."""
    
    PHASES = [
        ("scheduled", "Scheduled"),
        ("parsing", "Parsing Documents"),
        ("pii", "PII Detection & Masking"),
        ("ie", "Information Extraction (LLM)"),
        ("embedding", "Generating Embeddings"),
        ("weaviate", "Writing to Weaviate"),
        ("neo4j", "Writing to Neo4j"),
        ("reports", "Generating Reports"),
        ("completed", "Completed"),
        ("failed", "Failed")
    ]
    
    def __init__(self, job_id: str, domain: str, input_dir: str):
        """Initialize progress tracker for a job."""
        self.job_id = job_id
        self.domain = domain
        self.input_dir = input_dir
        self.start_time: Optional[datetime] = None
        self.current_phase_idx = 0
        self.files_total = 0
        self.files_done = 0
        self.blocks_extracted = 0
        self.entities_found = 0
        self.relationships_found = 0
        self.images_processed = 0
        self.weaviate_chunks = 0
        self.neo4j_nodes = 0
        self.current_file = ""
        self.error: Optional[str] = None
    
    def start(self):
        """Mark job as started."""
        self.start_time = datetime.now(timezone.utc)
        self._write_progress()
    
    def set_phase(self, phase: str):
        """Set current phase by name."""
        for idx, (phase_key, _) in enumerate(self.PHASES):
            if phase_key == phase:
                self.current_phase_idx = idx
                break
        self._write_progress()
    
    def set_files_total(self, total: int):
        """Set total number of files to process."""
        self.files_total = total
        self._write_progress()
    
    def file_done(self, filename: str, blocks: int = 0):
        """Mark a file as processed."""
        self.files_done += 1
        self.current_file = filename
        self.blocks_extracted += blocks
        self._write_progress()
    
    def add_entities(self, count: int):
        """Add to entity count."""
        self.entities_found += count
        self._write_progress()

    def set_counts(self, entities: int = None, relationships: int = None, images: int = None,
                   blocks: int = None, weaviate: int = None, neo4j: int = None):
        """Set counts for various metrics."""
        if entities is not None:
            self.entities_found = entities
        if relationships is not None:
            self.relationships_found = relationships
        if images is not None:
            self.images_processed = images
        if blocks is not None:
            self.blocks_extracted = blocks
        if weaviate is not None:
            self.weaviate_chunks = weaviate
        if neo4j is not None:
            self.neo4j_nodes = neo4j
        self._write_progress()

    def add_images(self, count: int):
        """Add to image count."""
        self.images_processed += count
        self._write_progress()
    
    def complete(self, report_paths: dict[str, str] = None):
        """Mark job as completed."""
        self.current_phase_idx = len(self.PHASES) - 2  # "completed"
        self._write_progress(report_paths=report_paths)
    
    def fail(self, error: str):
        """Mark job as failed."""
        self.error = error
        self.current_phase_idx = len(self.PHASES) - 1  # "failed"
        self._write_progress()
    
    def _write_progress(self, report_paths: dict[str, str] = None):
        """Write current progress to JSON file."""
        elapsed_sec = 0
        if self.start_time:
            elapsed_sec = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        
        phase_key, phase_name = self.PHASES[self.current_phase_idx]
        
        # Estimate ETA based on phase
        eta_sec = None
        if phase_key not in ("completed", "failed", "scheduled"):
            # Rough estimate: parsing ~2min, ie ~15min, others ~2min each
            phase_estimates = {
                "parsing": 120,
                "pii": 60,
                "ie": 900,  # 15 minutes for LLM
                "embedding": 120,
                "weaviate": 180,
                "neo4j": 180,
                "reports": 60
            }
            remaining_phases = [p[0] for p in self.PHASES[self.current_phase_idx:-2]]
            eta_sec = sum(phase_estimates.get(p, 60) for p in remaining_phases)
        
        progress_data = {
            "job_id": self.job_id,
            "domain": self.domain,
            "input_dir": self.input_dir,
            "status": phase_key,
            "phase": phase_name,
            "phase_number": self.current_phase_idx + 1,
            "total_phases": len(self.PHASES) - 2,  # Exclude completed/failed
            "progress_pct": min(95, int((self.current_phase_idx / (len(self.PHASES) - 2)) * 100)),
            "files_done": self.files_done,
            "files_total": self.files_total,
            "current_file": self.current_file,
            "blocks_extracted": self.blocks_extracted,
            "entities_found": self.entities_found,
            "relationships_found": self.relationships_found,
            "images_processed": self.images_processed,
            "weaviate_chunks": self.weaviate_chunks,
            "neo4j_nodes": self.neo4j_nodes,
            "neo4j_relationships": self.relationships_found,
            "elapsed_sec": int(elapsed_sec),
            "eta_sec": eta_sec,
            "started_at": self.start_time.isoformat() if self.start_time else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "error": self.error
        }
        
        if report_paths:
            progress_data["report_paths"] = report_paths
            progress_data["progress_pct"] = 100
        
        try:
            with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
                json.dump(progress_data, f, indent=2)
        except Exception as e:
            print(f"[ProgressTracker] Failed to write progress: {e}", flush=True)
    
    @staticmethod
    def clear():
        """Clear the progress file."""
        try:
            if PROGRESS_FILE.exists():
                PROGRESS_FILE.unlink()
        except Exception:
            pass
    
    @staticmethod
    def read() -> Optional[dict[str, Any]]:
        """Read current progress from file."""
        try:
            if PROGRESS_FILE.exists():
                with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None


def get_current_progress() -> Optional[dict[str, Any]]:
    """Get current job progress (for API/UI use)."""
    return ProgressTracker.read()


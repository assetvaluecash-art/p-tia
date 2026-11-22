from __future__ import annotations

import os

from celery import Celery
from pathlib import Path

from kbfabric_ingest._common.env_config import load_dotenv_file, require_env
from kbfabric_ingest.ingest.ingestion_service import IngestionService
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)

load_dotenv_file()
celery_app = Celery(
    "kbfabric",
    broker=require_env("CELERY_BROKER_URL"),
    backend=require_env("CELERY_RESULT_BACKEND"),
)


@celery_app.task
def run_ingestion_task(input_dir: str, domain: str) -> dict:
    """
    Celery task wrapper around IngestionService.run_ingestion.
    """
    logger.info(
        "celery_ingestion_task_received",
        input_dir=input_dir,
        domain=domain,
    )
    service = IngestionService()
    summary = service.run_ingestion(input_dir=Path(input_dir), domain=domain)
    logger.info(
        "celery_ingestion_task_completed",
        input_dir=input_dir,
        domain=domain,
        blocks_total=summary.get("total_blocks", 0),
    )
    return summary



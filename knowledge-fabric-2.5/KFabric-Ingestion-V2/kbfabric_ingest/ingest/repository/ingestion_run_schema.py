from pathlib import Path
import os

import psycopg

from kbfabric_ingest.utils.logger import get_logger


class IngestionRunSchema:
    """Ensure durable ingestion-run audit storage exists."""

    SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "ingestion_run.sql"

    def __init__(self) -> None:
        self.logger = get_logger(__name__)

    def ensure(self) -> None:
        sql = self.SQL_PATH.read_text(encoding="utf-8")
        with psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ["POSTGRES_PORT"]),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        self.logger.info("ingestion_run.schema.ensure.completed")

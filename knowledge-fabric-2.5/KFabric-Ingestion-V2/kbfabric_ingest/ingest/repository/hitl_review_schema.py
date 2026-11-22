from pathlib import Path
import os

import psycopg

from kbfabric_ingest.utils.logger import get_logger


class HITLReviewSchema:
    """Ensure the HITL review and append-only event tables exist at bootstrap."""

    SQL_PATHS = (
        Path(__file__).resolve().parents[1] / "sql" / "hitl_review.sql",
        Path(__file__).resolve().parents[1] / "sql" / "hitl_review_event.sql",
    )

    def __init__(self) -> None:
        self.logger = get_logger(__name__)

    def ensure(self) -> None:
        self.logger.info("hitl.schema.ensure.start")
        statements = [path.read_text(encoding="utf-8") for path in self.SQL_PATHS]
        with psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ["POSTGRES_PORT"]),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
        ) as conn:
            with conn.cursor() as cur:
                for sql in statements:
                    cur.execute(sql)
        self.logger.info("hitl.schema.ensure.completed")

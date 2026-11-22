from pathlib import Path
import os

import psycopg

from kbfabric_ingest.utils.logger import get_logger


logger = get_logger(__name__)


class CustomerSchema:
    """Ensures the customer control-plane table exists."""

    SQL_PATH = (
        Path(__file__).resolve().parents[1]
        / "customer"
        / "sql"
        / "customer.sql"
    )

    def ensure(self) -> None:
        logger.info(
            "fabric_foundry.customer_schema.ensure.start",
            sql_path=str(self.SQL_PATH),
        )

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

        logger.info(
            "fabric_foundry.customer_schema.ensure.completed",
        )

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from kbfabric_ingest.utils.logger import get_logger


class IndustryOntologySchema:
    """Creates the Industry-to-Ontology association table."""

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

    def ensure(self) -> None:
        self.logger.info("industry.ontology.schema.ensure.start")

        sql_path = Path(__file__).resolve().parent / "sql" / "industry_ontology.sql"
        sql = sql_path.read_text(encoding="utf-8")

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)

        self.logger.info("industry.ontology.schema.ensure.completed")

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import psycopg

from kbfabric_ingest.utils.logger import get_logger


@dataclass(frozen=True)
class IndustryRecord:
    industry_id: str
    industry_name: str
    description: str | None
    status: str
    metadata: dict[str, Any]


class IndustryRepository:
    """PostgreSQL persistence for the Fabric Foundry industry registry."""

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

    def list_industries(
        self,
        *,
        active_only: bool = False,
    ) -> list[IndustryRecord]:
        self.logger.info(
            "industry.registry.list.start",
            active_only=active_only,
        )

        query = """
            SELECT
                industry_id,
                industry_name,
                description,
                status,
                metadata
            FROM industry
        """

        params: tuple[object, ...] = ()

        if active_only:
            query += """
                WHERE status = 'ACTIVE'
            """

        query += """
            ORDER BY industry_name
        """

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()

        industries = [
            IndustryRecord(
                industry_id=row[0],
                industry_name=row[1],
                description=row[2],
                status=row[3],
                metadata=row[4] or {},
            )
            for row in rows
        ]

        self.logger.info(
            "industry.registry.list.completed",
            count=len(industries),
            active_only=active_only,
        )

        return industries

    def get_industry(
        self,
        *,
        industry_id: str,
    ) -> IndustryRecord | None:
        normalized = industry_id.strip().lower()

        self.logger.info(
            "industry.registry.get.start",
            industry_id=normalized,
        )

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        industry_id,
                        industry_name,
                        description,
                        status,
                        metadata
                    FROM industry
                    WHERE industry_id = %s
                    """,
                    (normalized,),
                )
                row = cur.fetchone()

        if row is None:
            self.logger.info(
                "industry.registry.get.not_found",
                industry_id=normalized,
            )
            return None

        industry = IndustryRecord(
            industry_id=row[0],
            industry_name=row[1],
            description=row[2],
            status=row[3],
            metadata=row[4] or {},
        )

        self.logger.info(
            "industry.registry.get.completed",
            industry_id=industry.industry_id,
        )

        return industry

    def create_industry(
        self,
        *,
        industry_id: str,
        industry_name: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IndustryRecord:
        normalized_id = industry_id.strip().lower()
        normalized_name = industry_name.strip()

        if not normalized_id:
            raise ValueError("industry_id must not be empty")

        if not normalized_name:
            raise ValueError("industry_name must not be empty")

        metadata = metadata or {}

        self.logger.info(
            "industry.registry.create.start",
            industry_id=normalized_id,
            industry_name=normalized_name,
        )

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO industry (
                            industry_id,
                            industry_name,
                            description,
                            metadata
                        )
                        VALUES (%s, %s, %s, %s::jsonb)
                        RETURNING
                            industry_id,
                            industry_name,
                            description,
                            status,
                            metadata
                        """,
                        (
                            normalized_id,
                            normalized_name,
                            description,
                            json.dumps(metadata),
                        ),
                    )

                    row = cur.fetchone()
        except psycopg.errors.UniqueViolation as exc:
            self.logger.info(
                "industry.registry.create.duplicate",
                industry_id=normalized_id,
            )
            raise ValueError(
                f"Industry ID already exists: {normalized_id}"
            ) from exc

        if row is None:
            raise RuntimeError(
                "Industry creation succeeded without returning industry"
            )

        industry = IndustryRecord(
            industry_id=row[0],
            industry_name=row[1],
            description=row[2],
            status=row[3],
            metadata=row[4] or {},
        )

        self.logger.info(
            "industry.registry.create.completed",
            industry_id=industry.industry_id,
        )

        return industry

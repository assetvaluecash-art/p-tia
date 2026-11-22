from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg

from kbfabric_ingest.utils.logger import get_logger


@dataclass(frozen=True)
class IndustryOntologyRecord:
    industry_id: str
    ontology_id: str
    ontology_version: str
    status: str
    is_default: bool


class IndustryOntologyRepository:
    """PostgreSQL persistence for Industry-to-Ontology associations."""

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

    def associate(
        self,
        *,
        industry_id: str,
        ontology_id: str,
        ontology_version: str,
        is_default: bool = False,
    ) -> IndustryOntologyRecord:
        normalized_industry_id = industry_id.strip().lower()
        normalized_ontology_id = ontology_id.strip().lower()
        normalized_version = ontology_version.strip()

        if not normalized_industry_id:
            raise ValueError("industry_id must not be empty")

        if not normalized_ontology_id:
            raise ValueError("ontology_id must not be empty")

        if not normalized_version:
            raise ValueError("ontology_version must not be empty")

        self.logger.info(
            "industry.ontology.association.create.start",
            industry_id=normalized_industry_id,
            ontology_id=normalized_ontology_id,
            ontology_version=normalized_version,
            is_default=is_default,
        )

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO industry_ontology (
                            industry_id,
                            ontology_id,
                            ontology_version,
                            is_default
                        )
                        VALUES (%s, %s, %s, %s)
                        RETURNING
                            industry_id,
                            ontology_id,
                            ontology_version,
                            status,
                            is_default
                        """,
                        (
                            normalized_industry_id,
                            normalized_ontology_id,
                            normalized_version,
                            is_default,
                        ),
                    )

                    row = cur.fetchone()
        except psycopg.errors.UniqueViolation as exc:
            self.logger.info(
                "industry.ontology.association.create.duplicate",
                industry_id=normalized_industry_id,
                ontology_id=normalized_ontology_id,
                ontology_version=normalized_version,
            )
            raise ValueError(
                "Industry is already associated with ontology version: "
                f"{normalized_industry_id} / "
                f"{normalized_ontology_id} / "
                f"{normalized_version}"
            ) from exc

        if row is None:
            raise RuntimeError(
                "Industry ontology association succeeded "
                "without returning association"
            )

        association = IndustryOntologyRecord(
            industry_id=row[0],
            ontology_id=row[1],
            ontology_version=row[2],
            status=row[3],
            is_default=row[4],
        )

        self.logger.info(
            "industry.ontology.association.create.completed",
            industry_id=association.industry_id,
            ontology_id=association.ontology_id,
            ontology_version=association.ontology_version,
        )

        return association

    def list_for_industry(
        self,
        *,
        industry_id: str,
        active_only: bool = False,
    ) -> list[IndustryOntologyRecord]:
        normalized_industry_id = industry_id.strip().lower()

        self.logger.info(
            "industry.ontology.association.list.start",
            industry_id=normalized_industry_id,
            active_only=active_only,
        )

        query = """
            SELECT
                industry_id,
                ontology_id,
                ontology_version,
                status,
                is_default
            FROM industry_ontology
            WHERE industry_id = %s
        """

        params: list[object] = [normalized_industry_id]

        if active_only:
            query += """
                AND status = 'ACTIVE'
            """

        query += """
            ORDER BY is_default DESC, ontology_id, ontology_version
        """

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall()

        associations = [
            IndustryOntologyRecord(
                industry_id=row[0],
                ontology_id=row[1],
                ontology_version=row[2],
                status=row[3],
                is_default=row[4],
            )
            for row in rows
        ]

        self.logger.info(
            "industry.ontology.association.list.completed",
            industry_id=normalized_industry_id,
            count=len(associations),
        )

        return associations

    def get_association(
        self,
        *,
        industry_id: str,
        ontology_id: str,
        ontology_version: str,
    ) -> IndustryOntologyRecord | None:
        normalized_industry_id = industry_id.strip().lower()
        normalized_ontology_id = ontology_id.strip().lower()
        normalized_version = ontology_version.strip()

        self.logger.info(
            "industry.ontology.association.get.start",
            industry_id=normalized_industry_id,
            ontology_id=normalized_ontology_id,
            ontology_version=normalized_version,
        )

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        industry_id,
                        ontology_id,
                        ontology_version,
                        status,
                        is_default
                    FROM industry_ontology
                    WHERE industry_id = %s
                      AND ontology_id = %s
                      AND ontology_version = %s
                    """,
                    (
                        normalized_industry_id,
                        normalized_ontology_id,
                        normalized_version,
                    ),
                )
                row = cur.fetchone()

        if row is None:
            self.logger.info(
                "industry.ontology.association.get.not_found",
                industry_id=normalized_industry_id,
                ontology_id=normalized_ontology_id,
                ontology_version=normalized_version,
            )
            return None

        association = IndustryOntologyRecord(
            industry_id=row[0],
            ontology_id=row[1],
            ontology_version=row[2],
            status=row[3],
            is_default=row[4],
        )

        self.logger.info(
            "industry.ontology.association.get.completed",
            industry_id=association.industry_id,
            ontology_id=association.ontology_id,
            ontology_version=association.ontology_version,
        )

        return association

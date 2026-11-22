from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import psycopg

from kbfabric_ingest.utils.logger import get_logger


@dataclass(frozen=True)
class OntologyRecord:
    ontology_id: str
    ontology_name: str
    version: str
    status: str
    artifact_path: str
    metadata: dict[str, Any]


class OntologyRepository:
    """PostgreSQL persistence for canonical ontology metadata."""

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

    def create_ontology(
        self,
        *,
        ontology_id: str,
        ontology_name: str,
        version: str,
        artifact_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> OntologyRecord:
        normalized_id = ontology_id.strip().lower()
        normalized_name = ontology_name.strip()
        normalized_version = version.strip()
        normalized_path = artifact_path.strip()

        if not normalized_id:
            raise ValueError("ontology_id must not be empty")

        if not normalized_name:
            raise ValueError("ontology_name must not be empty")

        if not normalized_version:
            raise ValueError("version must not be empty")

        if not normalized_path:
            raise ValueError("artifact_path must not be empty")

        metadata = metadata or {}

        self.logger.info(
            "ontology.registry.create.start",
            ontology_id=normalized_id,
            version=normalized_version,
        )

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ontology (
                            ontology_id,
                            ontology_name,
                            version,
                            artifact_path,
                            metadata
                        )
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        RETURNING
                            ontology_id,
                            ontology_name,
                            version,
                            status,
                            artifact_path,
                            metadata
                        """,
                        (
                            normalized_id,
                            normalized_name,
                            normalized_version,
                            normalized_path,
                            json.dumps(metadata),
                        ),
                    )

                    row = cur.fetchone()
        except psycopg.errors.UniqueViolation as exc:
            self.logger.info(
                "ontology.registry.create.duplicate",
                ontology_id=normalized_id,
                version=normalized_version,
            )
            raise ValueError(
                "Ontology version already exists: "
                f"{normalized_id} {normalized_version}"
            ) from exc

        if row is None:
            raise RuntimeError(
                "Ontology creation succeeded without returning ontology"
            )

        ontology = OntologyRecord(
            ontology_id=row[0],
            ontology_name=row[1],
            version=row[2],
            status=row[3],
            artifact_path=row[4],
            metadata=row[5] or {},
        )

        self.logger.info(
            "ontology.registry.create.completed",
            ontology_id=ontology.ontology_id,
            version=ontology.version,
        )

        return ontology

    def get_ontology(
        self,
        *,
        ontology_id: str,
        version: str | None = None,
    ) -> OntologyRecord | None:
        normalized_id = ontology_id.strip().lower()

        self.logger.info(
            "ontology.registry.get.start",
            ontology_id=normalized_id,
            version=version,
        )

        query = """
            SELECT
                ontology_id,
                ontology_name,
                version,
                status,
                artifact_path,
                metadata
            FROM ontology
            WHERE ontology_id = %s
        """

        params: list[object] = [normalized_id]

        if version is not None:
            query += """
                AND version = %s
            """
            params.append(version.strip())

        query += """
            ORDER BY version
            LIMIT 1
        """

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                row = cur.fetchone()

        if row is None:
            self.logger.info(
                "ontology.registry.get.not_found",
                ontology_id=normalized_id,
                version=version,
            )
            return None

        ontology = OntologyRecord(
            ontology_id=row[0],
            ontology_name=row[1],
            version=row[2],
            status=row[3],
            artifact_path=row[4],
            metadata=row[5] or {},
        )

        self.logger.info(
            "ontology.registry.get.completed",
            ontology_id=ontology.ontology_id,
            version=ontology.version,
        )

        return ontology

    def list_ontologies(
        self,
        *,
        ontology_id: str | None = None,
        active_only: bool = False,
    ) -> list[OntologyRecord]:
        self.logger.info(
            "ontology.registry.list.start",
            ontology_id=ontology_id,
            active_only=active_only,
        )

        query = """
            SELECT
                ontology_id,
                ontology_name,
                version,
                status,
                artifact_path,
                metadata
            FROM ontology
            WHERE 1 = 1
        """

        params: list[object] = []

        if ontology_id is not None:
            query += """
                AND ontology_id = %s
            """
            params.append(ontology_id.strip().lower())

        if active_only:
            query += """
                AND status = 'ACTIVE'
            """

        query += """
            ORDER BY ontology_id, version
        """

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall()

        ontologies = [
            OntologyRecord(
                ontology_id=row[0],
                ontology_name=row[1],
                version=row[2],
                status=row[3],
                artifact_path=row[4],
                metadata=row[5] or {},
            )
            for row in rows
        ]

        self.logger.info(
            "ontology.registry.list.completed",
            count=len(ontologies),
        )

        return ontologies

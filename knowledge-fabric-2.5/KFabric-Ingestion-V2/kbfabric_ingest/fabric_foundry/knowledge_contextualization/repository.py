from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from kbfabric_ingest.fabric_foundry.customer.repository import (
    CustomerRepository,
)
from kbfabric_ingest.fabric_foundry.industry.ontology_repository import (
    IndustryOntologyRepository,
)
from kbfabric_ingest.utils.logger import get_logger


logger = get_logger(__name__)


class CustomerOntologyAlreadyExistsError(Exception):
    """Raised when a customer ontology version already exists."""


class CustomerOntologyNotFoundError(Exception):
    """Raised when a customer ontology cannot be found."""


class CustomerOntologyCanonicalNotAllocatedError(Exception):
    """Raised when the canonical ontology is not allocated to the customer's industry."""


class CustomerOntologyInvalidVersionError(ValueError):
    """Raised when a customer ontology version is invalid."""


class CustomerOntologyVersionConflictError(Exception):
    """Raised when the active customer ontology changed since the request base version."""


@dataclass(frozen=True)
class CustomerOntologyRecord:
    customer_ontology_id: UUID
    tenant_id: str
    customer_id: str
    ontology_id: str
    ontology_version: str
    customer_ontology_version: int
    status: str
    definition: dict
    change_type: str
    change_reason: str | None
    change_summary: dict
    parent_customer_ontology_id: UUID | None
    created_by: str
    created_at: object
    confirmed_by: str | None
    confirmed_at: object | None
    updated_at: object
    request_id: str | None = None


class CustomerOntologyRepository:
    def __init__(
        self,
        customer_repository: CustomerRepository | None = None,
        industry_ontology_repository: IndustryOntologyRepository | None = None,
    ) -> None:
        self.customer_repository = (
            customer_repository or CustomerRepository()
        )
        self.industry_ontology_repository = (
            industry_ontology_repository
            or IndustryOntologyRepository()
        )

    def _connect(self):
        return self.customer_repository._connect()

    @staticmethod
    def _record_from_row(row) -> CustomerOntologyRecord:
        if row is None:
            raise CustomerOntologyNotFoundError(
                "Customer ontology row not found"
            )

        # Backward-compatible with the original 17-column row shape.
        if len(row) == 17:
            (
                customer_ontology_id,
                tenant_id,
                customer_id,
                ontology_id,
                ontology_version,
                customer_ontology_version,
                status,
                definition,
                change_type,
                change_reason,
                change_summary,
                parent_customer_ontology_id,
                created_by,
                created_at,
                confirmed_by,
                confirmed_at,
                updated_at,
            ) = row
            request_id = None

        # New persisted row shape includes request_id after change_reason.
        elif len(row) == 18:
            (
                customer_ontology_id,
                tenant_id,
                customer_id,
                ontology_id,
                ontology_version,
                customer_ontology_version,
                status,
                definition,
                change_type,
                change_reason,
                request_id,
                change_summary,
                parent_customer_ontology_id,
                created_by,
                created_at,
                confirmed_by,
                confirmed_at,
                updated_at,
            ) = row

        else:
            raise ValueError(
                "Unexpected customer_ontology row shape: "
                f"{len(row)} columns"
            )

        return CustomerOntologyRecord(
            customer_ontology_id=customer_ontology_id,
            tenant_id=tenant_id,
            customer_id=customer_id,
            ontology_id=ontology_id,
            ontology_version=ontology_version,
            customer_ontology_version=customer_ontology_version,
            status=status,
            definition=definition,
            change_type=change_type,
            change_reason=change_reason,
            request_id=request_id,
            change_summary=change_summary,
            parent_customer_ontology_id=parent_customer_ontology_id,
            created_by=created_by,
            created_at=created_at,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
            updated_at=updated_at,
        )

    def create_customer_ontology(
        self,
        *,
        customer_id: str,
        ontology_id: str,
        ontology_version: str,
        definition: dict,
        customer_ontology_version: int = 1,
        status: str = "ACTIVE",
        change_type: str = "INITIAL",
        change_reason: str | None = None,
        change_summary: dict | None = None,
        parent_customer_ontology_id=None,
        created_by: str = "SYSTEM",
        confirmed_by: str | None = None,
        confirmed_at: object | None = None,
    ) -> CustomerOntologyRecord:
        customer_id = customer_id.strip()
        ontology_id = ontology_id.strip()
        ontology_version = ontology_version.strip()
        change_type = change_type.strip().upper()
        created_by = created_by.strip()

        if not customer_id:
            raise ValueError("customer_id must not be empty")

        if not ontology_id:
            raise ValueError("ontology_id must not be empty")

        if not ontology_version:
            raise ValueError("ontology_version must not be empty")

        if not isinstance(customer_ontology_version, int):
            raise CustomerOntologyInvalidVersionError(
                "customer_ontology_version must be a positive integer"
            )

        if customer_ontology_version <= 0:
            raise CustomerOntologyInvalidVersionError(
                "customer_ontology_version must be a positive integer"
            )

        if not isinstance(definition, dict):
            raise ValueError("definition must be a dictionary")

        if change_summary is None:
            change_summary = {}

        if not isinstance(change_summary, dict):
            raise ValueError("change_summary must be a dictionary")

        customer = self.customer_repository.get_by_customer_id(
            customer_id=customer_id
        )

        if customer is None:
            raise CustomerOntologyNotFoundError(
                f"Customer not found: {customer_id}"
            )

        association = self.industry_ontology_repository.get_association(
            industry_id=customer.industry_id,
            ontology_id=ontology_id,
            ontology_version=ontology_version,
        )

        if association is None or association.status != "ACTIVE":
            raise CustomerOntologyCanonicalNotAllocatedError(
                "Canonical ontology is not actively allocated to customer industry: "
                f"{customer.industry_id}/{ontology_id}/{ontology_version}"
            )

        customer_ontology_id = uuid4()

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO customer_ontology (
                        customer_ontology_id,
                        tenant_id,
                        customer_id,
                        ontology_id,
                        ontology_version,
                        customer_ontology_version,
                        status,
                        definition,
                        change_type,
                        change_reason,
                        change_summary,
                        parent_customer_ontology_id,
                        created_by,
                        confirmed_by,
                        confirmed_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING
                        customer_ontology_id,
                        tenant_id,
                        customer_id,
                        ontology_id,
                        ontology_version,
                        customer_ontology_version,
                        status,
                        definition,
                        change_type,
                        change_reason,
                        change_summary,
                        parent_customer_ontology_id,
                        created_by,
                        created_at,
                        confirmed_by,
                        confirmed_at,
                        updated_at
                    """,
                    (
                        customer_ontology_id,
                        customer.tenant_id,
                        customer.customer_id,
                        ontology_id,
                        ontology_version,
                        customer_ontology_version,
                        status,
                        Jsonb(definition),
                        change_type,
                        change_reason,
                        Jsonb(change_summary),
                        parent_customer_ontology_id,
                        created_by,
                        confirmed_by,
                        confirmed_at,
                    ),
                )

                row = cur.fetchone()

        record = self._record_from_row(row)

        logger.info(
            "knowledge_contextualization.customer_ontology.create.completed",
            customer_id=customer_id,
            tenant_id=record.tenant_id,
            customer_ontology_id=str(record.customer_ontology_id),
            customer_ontology_version=record.customer_ontology_version,
        )

        return record

    def get_active(
        self,
        customer_id: str,
    ) -> CustomerOntologyRecord | None:
        customer_id = customer_id.strip()

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        customer_ontology_id,
                        tenant_id,
                        customer_id,
                        ontology_id,
                        ontology_version,
                        customer_ontology_version,
                        status,
                        definition,
                        change_type,
                        change_reason,
                        request_id,
                        change_summary,
                        parent_customer_ontology_id,
                        created_by,
                        created_at,
                        confirmed_by,
                        confirmed_at,
                        updated_at
                    FROM customer_ontology
                    WHERE customer_id = %s
                      AND status = 'ACTIVE'
                    ORDER BY customer_ontology_version DESC
                    LIMIT 1
                    """,
                    (customer_id,),
                )

                row = cur.fetchone()

        if row is None:
            return None

        return self._record_from_row(row)

    def get_by_version(
        self, customer_id: str, customer_ontology_version: int
    ) -> CustomerOntologyRecord | None:
        """Load an immutable customer ontology snapshot by its version number."""
        customer_id = customer_id.strip()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        customer_ontology_id, tenant_id, customer_id, ontology_id,
                        ontology_version, customer_ontology_version, status, definition,
                        change_type, change_reason, request_id, change_summary,
                        parent_customer_ontology_id, created_by, created_at,
                        confirmed_by, confirmed_at, updated_at
                    FROM customer_ontology
                    WHERE customer_id = %s AND customer_ontology_version = %s
                    LIMIT 1
                    """,
                    (customer_id, customer_ontology_version),
                )
                row = cur.fetchone()
        return self._record_from_row(row) if row is not None else None

    def get_by_request_id(self, customer_id: str, request_id: str) -> CustomerOntologyRecord | None:
        customer_id = customer_id.strip()
        request_id = request_id.strip()

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        customer_ontology_id,
                        tenant_id,
                        customer_id,
                        ontology_id,
                        ontology_version,
                        customer_ontology_version,
                        status,
                        definition,
                        change_type,
                        change_reason,
                        request_id,
                        change_summary,
                        parent_customer_ontology_id,
                        created_by,
                        created_at,
                        confirmed_by,
                        confirmed_at,
                        updated_at
                    FROM customer_ontology
                    WHERE customer_id = %s
                      AND request_id = %s
                    LIMIT 1
                    """,
                    (customer_id, request_id),
                )

                row = cur.fetchone()

        if row is None:
            return None

        return self._record_from_row(row)

    def list_history(
        self,
        customer_id: str,
    ) -> list[CustomerOntologyRecord]:
        customer_id = customer_id.strip()

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        customer_ontology_id,
                        tenant_id,
                        customer_id,
                        ontology_id,
                        ontology_version,
                        customer_ontology_version,
                        status,
                        definition,
                        change_type,
                        change_reason,
                        request_id,
                        change_summary,
                        parent_customer_ontology_id,
                        created_by,
                        created_at,
                        confirmed_by,
                        confirmed_at,
                        updated_at
                    FROM customer_ontology
                    WHERE customer_id = %s
                    ORDER BY customer_ontology_version DESC
                    """,
                    (customer_id,),
                )

                rows = cur.fetchall()

        return [self._record_from_row(row) for row in rows]

    def list_versions(
        self,
        customer_id: str,
    ) -> list[CustomerOntologyRecord]:
        return self.list_history(customer_id)

    def create_next_version(
        self,
        customer_id: str,
        definition: dict,
        *,
        base_customer_ontology_version: int,
        request_id: str,
        change_type: str = "CONTEXTUALIZATION",
        change_reason: str | None = None,
        change_summary: dict | None = None,
        created_by: str = "SYSTEM",
        confirmed_by: str | None = None,
        confirmed_at: object | None = None,
    ) -> CustomerOntologyRecord:
        customer_id = customer_id.strip()
        change_type = change_type.strip().upper()
        created_by = created_by.strip()
        request_id = request_id.strip()

        if not customer_id:
            raise ValueError("customer_id must not be empty")

        if not isinstance(base_customer_ontology_version, int):
            raise CustomerOntologyInvalidVersionError(
                "base_customer_ontology_version must be a positive integer"
            )

        if base_customer_ontology_version <= 0:
            raise CustomerOntologyInvalidVersionError(
                "base_customer_ontology_version must be a positive integer"
            )

        if not request_id:
            raise ValueError("request_id must not be empty")

        if not isinstance(definition, dict):
            raise ValueError("definition must be a dictionary")

        if change_type not in {
            "CONTEXTUALIZATION",
            "ACTIVATION",
            "DEACTIVATION",
            "ALIAS",
            "ADDITION",
            "MIXED",
        }:
            raise ValueError(
                "change_type must be one of "
                "CONTEXTUALIZATION, ACTIVATION, DEACTIVATION, "
                "ALIAS, ADDITION, MIXED"
            )

        if not created_by:
            raise ValueError("created_by must not be empty")

        if change_summary is None:
            change_summary = {}

        if not isinstance(change_summary, dict):
            raise ValueError("change_summary must be a dictionary")

        logger.info(
            "knowledge_contextualization.customer_ontology.version.create.start",
            customer_id=customer_id,
            change_type=change_type,
            created_by=created_by,
            request_id=request_id,
            base_customer_ontology_version=base_customer_ontology_version,
        )

        customer = self.customer_repository.get_by_customer_id(
            customer_id=customer_id
        )

        if customer is None:
            raise CustomerOntologyNotFoundError(
                f"Customer not found: {customer_id}"
            )

        customer_ontology_id = uuid4()

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:

                    # Idempotency: replaying the same request returns the
                    # already-created version without creating another one.
                    cur.execute(
                        """
                        SELECT
                            customer_ontology_id,
                            tenant_id,
                            customer_id,
                            ontology_id,
                            ontology_version,
                            customer_ontology_version,
                            status,
                            definition,
                            change_type,
                            change_reason,
                            request_id,
                            change_summary,
                            parent_customer_ontology_id,
                            created_by,
                            created_at,
                            confirmed_by,
                            confirmed_at,
                            updated_at
                        FROM customer_ontology
                        WHERE tenant_id = %s
                          AND request_id = %s
                        LIMIT 1
                        """,
                        (customer.tenant_id, request_id),
                    )

                    replay_row = cur.fetchone()

                    if replay_row is not None:
                        replay_record = self._record_from_row(replay_row)

                        logger.info(
                            "knowledge_contextualization.customer_ontology.version.create.replay",
                            customer_id=customer_id,
                            request_id=request_id,
                            customer_ontology_version=(
                                replay_record.customer_ontology_version
                            ),
                        )

                        return replay_record

                    # Lock the current active version before checking the
                    # optimistic-concurrency base version.
                    cur.execute(
                        """
                        SELECT
                            customer_ontology_id,
                            tenant_id,
                            customer_id,
                            ontology_id,
                            ontology_version,
                            customer_ontology_version,
                            status,
                            definition,
                            change_type,
                            change_reason,
                            request_id,
                            change_summary,
                            parent_customer_ontology_id,
                            created_by,
                            created_at,
                            confirmed_by,
                            confirmed_at,
                            updated_at
                        FROM customer_ontology
                        WHERE customer_id = %s
                          AND status = 'ACTIVE'
                        ORDER BY customer_ontology_version DESC
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (customer_id,),
                    )

                    current_row = cur.fetchone()

                    if current_row is None:
                        raise CustomerOntologyNotFoundError(
                            f"No active customer ontology found: {customer_id}"
                        )

                    current_record = self._record_from_row(current_row)

                    if (
                        current_record.customer_ontology_version
                        != base_customer_ontology_version
                    ):
                        logger.info(
                            "knowledge_contextualization.customer_ontology.version.create.conflict",
                            customer_id=customer_id,
                            request_id=request_id,
                            base_customer_ontology_version=(
                                base_customer_ontology_version
                            ),
                            current_customer_ontology_version=(
                                current_record.customer_ontology_version
                            ),
                        )

                        raise CustomerOntologyVersionConflictError(
                            "Customer ontology version conflict: "
                            f"base={base_customer_ontology_version}, "
                            f"current={current_record.customer_ontology_version}"
                        )

                    next_version = (
                        current_record.customer_ontology_version + 1
                    )

                    cur.execute(
                        """
                        UPDATE customer_ontology
                        SET
                            status = 'INACTIVE',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE customer_ontology_id = %s
                          AND status = 'ACTIVE'
                        """,
                        (current_record.customer_ontology_id,),
                    )

                    cur.execute(
                        """
                        INSERT INTO customer_ontology (
                            customer_ontology_id,
                            tenant_id,
                            customer_id,
                            ontology_id,
                            ontology_version,
                            customer_ontology_version,
                            status,
                            definition,
                            change_type,
                            change_reason,
                            request_id,
                            change_summary,
                            parent_customer_ontology_id,
                            created_by,
                            confirmed_by,
                            confirmed_at
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        RETURNING
                            customer_ontology_id,
                            tenant_id,
                            customer_id,
                            ontology_id,
                            ontology_version,
                            customer_ontology_version,
                            status,
                            definition,
                            change_type,
                            change_reason,
                            request_id,
                            change_summary,
                            parent_customer_ontology_id,
                            created_by,
                            created_at,
                            confirmed_by,
                            confirmed_at,
                            updated_at
                        """,
                        (
                            customer_ontology_id,
                            current_record.tenant_id,
                            current_record.customer_id,
                            current_record.ontology_id,
                            current_record.ontology_version,
                            next_version,
                            "ACTIVE",
                            Jsonb(definition),
                            change_type,
                            change_reason,
                            request_id,
                            Jsonb(change_summary),
                            current_record.customer_ontology_id,
                            created_by,
                            confirmed_by,
                            confirmed_at,
                        ),
                    )

                    row = cur.fetchone()

        except UniqueViolation as exc:
            logger.info(
                "knowledge_contextualization.customer_ontology.version.create.duplicate",
                customer_id=customer_id,
                request_id=request_id,
                next_version=(
                    next_version if "next_version" in locals() else None
                ),
            )

            raise CustomerOntologyAlreadyExistsError(
                "Customer ontology version already exists"
            ) from exc

        record = self._record_from_row(row)

        logger.info(
            "knowledge_contextualization.customer_ontology.version.create.completed",
            customer_id=customer_id,
            tenant_id=record.tenant_id,
            customer_ontology_id=str(record.customer_ontology_id),
            customer_ontology_version=record.customer_ontology_version,
            request_id=record.request_id,
            parent_customer_ontology_id=str(
                record.parent_customer_ontology_id
            ),
            change_type=record.change_type,
        )

        return record

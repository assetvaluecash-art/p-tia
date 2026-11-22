import os
import uuid

import psycopg
from psycopg.errors import UniqueViolation

from kbfabric_ingest.fabric_foundry.customer.context import TenantContext
from kbfabric_ingest.fabric_foundry.industry.repository import IndustryRepository
from kbfabric_ingest.utils.logger import get_logger


logger = get_logger(__name__)


class CustomerAlreadyExistsError(ValueError):
    pass


class CustomerIndustryNotFoundError(ValueError):
    pass


class CustomerRepository:
    def __init__(
        self,
        *,
        industry_repository: IndustryRepository | None = None,
    ) -> None:
        self.industry_repository = industry_repository or IndustryRepository()

    @staticmethod
    def _generate_customer_id(customer_name: str, tenant_id: str) -> str:
        return f"Pulse AI_{customer_name.strip()}_{tenant_id}"

    @staticmethod
    def _connect():
        return psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ["POSTGRES_PORT"]),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
        )

    def create_customer(
        self,
        *,
        customer_name: str,
        industry_id: str,
        customer_id: str | None = None,
        created_by: str,
    ) -> TenantContext:
        customer_name = customer_name.strip()
        industry_id = industry_id.strip()
        created_by = created_by.strip()

        if customer_id is not None:
            customer_id = customer_id.strip()
            if not customer_id:
                raise ValueError("customer_id must not be empty")
        if not customer_name:
            raise ValueError("customer_name must not be empty")
        if not industry_id:
            raise ValueError("industry_id must not be empty")
        if not created_by:
            raise ValueError("created_by must not be empty")

        logger.info(
            "customer.onboarding.create.start",
            customer_id=customer_id,
            industry_id=industry_id,
        )

        industry = self.industry_repository.get_industry(
            industry_id=industry_id
        )

        if industry is None or industry.status != "ACTIVE":
            logger.info(
                "customer.onboarding.create.industry_not_found",
                customer_id=customer_id,
                industry_id=industry_id,
            )
            raise CustomerIndustryNotFoundError(
                f"Active industry not found: {industry_id}"
            )

        tenant_id = str(uuid.uuid4())
        customer_id = customer_id or self._generate_customer_id(customer_name, tenant_id)

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO customer (
                            tenant_id,
                            customer_id,
                            customer_name,
                            industry_id,
                            created_by,
                            updated_by
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            tenant_id,
                            customer_id,
                            customer_name,
                            industry_id,
                            created_by,
                            created_by,
                        ),
                    )
        except UniqueViolation as exc:
            logger.info(
                "customer.onboarding.create.duplicate",
                customer_id=customer_id,
            )
            raise CustomerAlreadyExistsError(
                f"Customer already exists: {customer_id}"
            ) from exc

        context = TenantContext(
            tenant_id=tenant_id,
            customer_id=customer_id,
            customer_name=customer_name,
            industry_id=industry_id,
        )

        logger.info(
            "customer.onboarding.create.completed",
            created_by=created_by,
            tenant_id=tenant_id,
            customer_id=customer_id,
            industry_id=industry_id,
        )

        return context


    def list_active_customers(self) -> list[TenantContext]:
        """Return active customer workspaces for an authenticated UI context picker."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT tenant_id, customer_id, customer_name, industry_id
                    FROM customer
                    WHERE status = 'ACTIVE'
                    ORDER BY customer_name, customer_id
                    """
                )
                rows = cur.fetchall()
        return [
            TenantContext(
                tenant_id=row[0],
                customer_id=row[1],
                customer_name=row[2],
                industry_id=row[3],
            )
            for row in rows
        ]

    def get_active_customer(self) -> TenantContext:
        """Return the sole ACTIVE customer for legacy callers without explicit scope."""
        logger.info("customer.context.resolve_active.start")

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        tenant_id,
                        customer_id,
                        customer_name,
                        industry_id
                    FROM customer
                    WHERE status = 'ACTIVE'
                    ORDER BY created_at ASC
                    """
                )
                rows = cur.fetchall()

        if not rows:
            logger.error("customer.context.resolve_active.not_found")
            raise ValueError("No ACTIVE customer is configured")

        if len(rows) > 1:
            logger.error(
                "customer.context.resolve_active.multiple",
                active_customer_count=len(rows),
            )
            raise ValueError(
                f"Expected exactly one ACTIVE customer; found {len(rows)}"
            )

        row = rows[0]
        context = TenantContext(
            tenant_id=row[0],
            customer_id=row[1],
            customer_name=row[2],
            industry_id=row[3],
        )

        logger.info(
            "customer.context.resolve_active.completed",
            tenant_id=context.tenant_id,
            customer_id=context.customer_id,
            industry_id=context.industry_id,
        )

        return context

    def get_by_customer_id(
        self,
        *,
        customer_id: str,
    ) -> TenantContext | None:
        customer_id = customer_id.strip()

        logger.info(
            "customer.onboarding.get.start",
            customer_id=customer_id,
        )

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        tenant_id,
                        customer_id,
                        customer_name,
                        industry_id
                    FROM customer
                    WHERE customer_id = %s
                      AND status = 'ACTIVE'
                    """,
                    (customer_id,),
                )
                row = cur.fetchone()

        if row is None:
            logger.info(
                "customer.onboarding.get.not_found",
                customer_id=customer_id,
            )
            return None

        context = TenantContext(
            tenant_id=row[0],
            customer_id=row[1],
            customer_name=row[2],
            industry_id=row[3],
        )

        logger.info(
            "customer.onboarding.get.completed",
            tenant_id=context.tenant_id,
            customer_id=context.customer_id,
            industry_id=context.industry_id,
        )

        return context

from __future__ import annotations

from kbfabric_ingest.fabric_foundry.customer.context import TenantContext
from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository


class CustomerContextService:
    """Provide the authoritative customer workspace context."""

    def __init__(
        self,
        repository: CustomerRepository | None = None,
    ) -> None:
        self.repository = repository or CustomerRepository()

    def get_customer_context(
        self,
        *,
        customer_id: str,
    ) -> TenantContext:
        """Resolve the active customer workspace context."""
        context = self.repository.get_by_customer_id(
            customer_id=customer_id,
        )

        if context is None:
            raise ValueError(
                f"Active customer not found: {customer_id}"
            )

        return context

from kbfabric_ingest.fabric_foundry.customer.context import TenantContext
from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository


class ActiveCustomerContextResolver:
    """Resolve an explicitly selected customer, with singleton fallback for legacy callers."""

    def __init__(
        self,
        *,
        customer_repository: CustomerRepository | None = None,
    ) -> None:
        self.customer_repository = (
            customer_repository or CustomerRepository()
        )

    def resolve(self, customer_id: str | None = None) -> TenantContext:
        """Resolve a named active customer; retain singleton fallback for legacy callers."""
        if customer_id is not None:
            customer = self.customer_repository.get_by_customer_id(
                customer_id=customer_id
            )
            if customer is None:
                raise ValueError(f"No ACTIVE customer found: {customer_id}")
            return customer
        return self.customer_repository.get_active_customer()

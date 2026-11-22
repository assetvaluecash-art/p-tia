from unittest.mock import Mock

import pytest

from kbfabric_ingest.fabric_foundry.customer.active_customer_context import (
    ActiveCustomerContextResolver,
)
from kbfabric_ingest.fabric_foundry.customer.context import TenantContext


class TestActiveCustomerContextResolver:
    """Verify explicit run customer resolution and legacy singleton fallback.

    Architecturally, every API ingestion run must select a specific active customer;
    legacy internal callers retain their old fallback until migrated.
    """

    def test_resolves_sole_active_customer(self):
        repository = Mock()
        repository.get_active_customer.return_value = TenantContext(
            tenant_id="tenant-1",
            customer_id="customer-1",
            customer_name="Acme",
            industry_id="banking",
        )

        context = ActiveCustomerContextResolver(
            customer_repository=repository,
        ).resolve()

        assert context.tenant_id == "tenant-1"
        assert context.customer_id == "customer-1"
        assert context.industry_id == "banking"
        repository.get_active_customer.assert_called_once_with()

    def test_fails_when_no_active_customer_exists(self):
        repository = Mock()
        repository.get_active_customer.side_effect = ValueError(
            "No ACTIVE customer is configured"
        )

        with pytest.raises(ValueError, match="No ACTIVE customer"):
            ActiveCustomerContextResolver(
                customer_repository=repository,
            ).resolve()

    def test_fails_when_multiple_active_customers_exist(self):
        repository = Mock()
        repository.get_active_customer.side_effect = ValueError(
            "Expected exactly one ACTIVE customer; found 2"
        )

        with pytest.raises(
            ValueError,
            match="exactly one ACTIVE customer",
        ):
            ActiveCustomerContextResolver(
                customer_repository=repository,
            ).resolve()

    def test_resolves_requested_active_customer_without_singleton_lookup(self):
        """Objective: verify explicit customer selection bypasses global singleton lookup.

        This matters because multiple customer workspaces must ingest independently.
        Invariant: resolution returns only the requested active customer and never calls
        the legacy get_active_customer singleton path.
        """
        repository = Mock()
        expected = TenantContext(
            tenant_id="tenant-2",
            customer_id="customer-2",
            customer_name="Second customer",
            industry_id="insurance",
        )
        repository.get_by_customer_id.return_value = expected

        context = ActiveCustomerContextResolver(
            customer_repository=repository,
        ).resolve(customer_id="customer-2")

        assert context == expected
        repository.get_by_customer_id.assert_called_once_with(customer_id="customer-2")
        repository.get_active_customer.assert_not_called()

    def test_requested_customer_must_be_active_and_exist(self):
        """Objective: verify ingestion rejects a missing or inactive selected customer.

        This matters because customer scope controls ontology and persistence isolation.
        Invariant: an unresolved requested customer fails closed without singleton fallback.
        """
        repository = Mock()
        repository.get_by_customer_id.return_value = None

        with pytest.raises(ValueError, match="No ACTIVE customer found: customer-2"):
            ActiveCustomerContextResolver(
                customer_repository=repository,
            ).resolve(customer_id="customer-2")

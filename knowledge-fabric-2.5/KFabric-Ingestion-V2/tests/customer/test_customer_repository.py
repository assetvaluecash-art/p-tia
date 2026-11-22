from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kbfabric_ingest.fabric_foundry.customer.context import TenantContext
from kbfabric_ingest.fabric_foundry.customer.repository import (
    CustomerIndustryNotFoundError,
    CustomerRepository,
)
from kbfabric_ingest.fabric_foundry.industry.repository import IndustryRecord


class FakeIndustryRepository:
    def __init__(self, industry=None):
        self.industry = industry

    def get_industry(self, *, industry_id):
        return self.industry


def _repository_with_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetchone_result=None,
    industry=None,
):
    repository = CustomerRepository(
        industry_repository=FakeIndustryRepository(industry)
    )

    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_result

    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor

    monkeypatch.setattr(repository, "_connect", lambda: connection)

    return repository, connection, cursor


def _active_banking_industry() -> IndustryRecord:
    return IndustryRecord(
        industry_id="banking",
        industry_name="Enterprise Banking",
        description="Banking industry",
        status="ACTIVE",
        metadata={},
    )


def test_create_customer_generates_internal_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        industry=_active_banking_industry(),
    )

    context = repository.create_customer(
        customer_id="CUST-001",
        customer_name="Acme Bank",
        industry_id="banking",
        created_by="test-user-001",
    )

    assert isinstance(context, TenantContext)
    assert context.customer_id == "CUST-001"
    assert context.customer_name == "Acme Bank"
    assert context.industry_id == "banking"

    args = cursor.execute.call_args.args
    assert "INSERT INTO customer" in args[0]
    assert args[1][1:] == (
        "CUST-001",
        "Acme Bank",
        "banking",
        "test-user-001",
        "test-user-001",
    )

    generated_tenant_id = args[1][0]
    assert generated_tenant_id
    assert len(generated_tenant_id) == 36


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("customer_id", ""),
        ("customer_name", ""),
        ("industry_id", ""),
    ],
)
def test_create_customer_rejects_empty_required_fields(
    field: str,
    value: str,
) -> None:
    repository = CustomerRepository(
        industry_repository=FakeIndustryRepository(
            _active_banking_industry()
        )
    )

    kwargs = {
        "customer_id": "CUST-001",
        "customer_name": "Acme Bank",
        "industry_id": "banking",
        "created_by": "test-user-001",
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        repository.create_customer(**kwargs)


def test_create_customer_rejects_unknown_industry() -> None:
    repository = CustomerRepository(
        industry_repository=FakeIndustryRepository(None)
    )

    with pytest.raises(CustomerIndustryNotFoundError):
        repository.create_customer(
            customer_id="CUST-001",
            customer_name="Acme Bank",
            industry_id="unknown",
            created_by="test-user-001",
        )


def test_create_customer_rejects_inactive_industry() -> None:
    inactive_industry = IndustryRecord(
        industry_id="banking",
        industry_name="Enterprise Banking",
        description="Banking industry",
        status="INACTIVE",
        metadata={},
    )

    repository = CustomerRepository(
        industry_repository=FakeIndustryRepository(inactive_industry)
    )

    with pytest.raises(CustomerIndustryNotFoundError):
        repository.create_customer(
            customer_id="CUST-001",
            customer_name="Acme Bank",
            industry_id="banking",
            created_by="test-user-001",
        )


def test_get_by_customer_id_returns_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchone_result=(
            "tenant-001",
            "CUST-001",
            "Acme Bank",
            "banking",
        ),
    )

    context = repository.get_by_customer_id(
        customer_id="CUST-001",
    )

    assert context == TenantContext(
        tenant_id="tenant-001",
        customer_id="CUST-001",
        customer_name="Acme Bank",
        industry_id="banking",
    )

    args = cursor.execute.call_args.args
    assert "FROM customer" in args[0]
    assert "status = 'ACTIVE'" in args[0]
    assert args[1] == ("CUST-001",)


def test_get_by_customer_id_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, _ = _repository_with_connection(
        monkeypatch,
        fetchone_result=None,
    )

    assert repository.get_by_customer_id(
        customer_id="DOES-NOT-EXIST",
    ) is None

from types import SimpleNamespace
from uuid import UUID

import pytest

from kbfabric_ingest.fabric_foundry.knowledge_contextualization.repository import (
    CustomerOntologyAlreadyExistsError,
    CustomerOntologyCanonicalNotAllocatedError,
    CustomerOntologyInvalidVersionError,
    CustomerOntologyRepository,
)


class FakeCursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeCursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return FakeCursorContext(self.cursor_obj)


def make_repository(
    *,
    customer=None,
    association=None,
    cursor=None,
):
    customer_repository = SimpleNamespace(
        get_by_customer_id=lambda customer_id: customer,
        _connect=lambda: FakeConnection(cursor or FakeCursor()),
    )

    industry_ontology_repository = SimpleNamespace(
        get_association=lambda industry_id, ontology_id, ontology_version: association,
    )

    return CustomerOntologyRepository(
        customer_repository=customer_repository,
        industry_ontology_repository=industry_ontology_repository,
    )


def make_row(
    *,
    customer_ontology_id,
    tenant_id="tenant-001",
    customer_id="CUST-001",
    ontology_id="banking",
    ontology_version="5.0.1",
    customer_ontology_version=1,
    status="ACTIVE",
    definition=None,
    change_type="INITIAL",
    change_reason=None,
    change_summary=None,
    parent_customer_ontology_id=None,
    created_by="SYSTEM",
    created_at=None,
    confirmed_by=None,
    confirmed_at=None,
    updated_at=None,
):
    return (
        customer_ontology_id,
        tenant_id,
        customer_id,
        ontology_id,
        ontology_version,
        customer_ontology_version,
        status,
        definition or {"ProductLine": []},
        change_type,
        change_reason,
        change_summary or {},
        parent_customer_ontology_id,
        created_by,
        created_at,
        confirmed_by,
        confirmed_at,
        updated_at,
    )


def test_create_customer_ontology():
    customer = SimpleNamespace(
        tenant_id="tenant-001",
        customer_id="CUST-001",
        customer_name="Acme Bank",
        industry_id="banking",
    )

    association = SimpleNamespace(
        industry_id="banking",
        ontology_id="banking",
        ontology_version="5.0.1",
        status="ACTIVE",
    )

    customer_ontology_id = UUID(
        "11111111-1111-1111-1111-111111111111"
    )

    cursor = FakeCursor(
        row=make_row(
            customer_ontology_id=customer_ontology_id,
            definition={
                "ProductLine": [
                    {
                        "id": "personal_banking",
                        "status": "ACTIVE",
                    }
                ]
            },
        )
    )

    repository = make_repository(
        customer=customer,
        association=association,
        cursor=cursor,
    )

    result = repository.create_customer_ontology(
        customer_id="CUST-001",
        ontology_id="banking",
        ontology_version="5.0.1",
        definition={
            "ProductLine": [
                {
                    "id": "personal_banking",
                    "status": "ACTIVE",
                }
            ]
        },
    )

    assert result.customer_ontology_id == customer_ontology_id
    assert result.tenant_id == "tenant-001"
    assert result.customer_id == "CUST-001"
    assert result.ontology_id == "banking"
    assert result.ontology_version == "5.0.1"
    assert result.customer_ontology_version == 1
    assert result.status == "ACTIVE"
    assert result.change_type == "INITIAL"
    assert result.change_reason is None
    assert result.change_summary == {}
    assert result.parent_customer_ontology_id is None
    assert result.created_by == "SYSTEM"

    assert len(cursor.executed) == 1

    sql, params = cursor.executed[0]

    assert "INSERT INTO customer_ontology" in sql
    assert params[1] == "tenant-001"
    assert params[2] == "CUST-001"
    assert params[3] == "banking"
    assert params[4] == "5.0.1"
    assert params[5] == 1
    assert params[6] == "ACTIVE"


def test_create_customer_ontology_persists_history_metadata():
    customer = SimpleNamespace(
        tenant_id="tenant-001",
        customer_id="CUST-001",
        customer_name="Acme Bank",
        industry_id="banking",
    )

    association = SimpleNamespace(
        industry_id="banking",
        ontology_id="banking",
        ontology_version="5.0.1",
        status="ACTIVE",
    )

    parent_id = UUID("11111111-1111-1111-1111-111111111111")
    current_id = UUID("22222222-2222-2222-2222-222222222222")

    cursor = FakeCursor(
        row=make_row(
            customer_ontology_id=current_id,
            customer_ontology_version=2,
            status="ACTIVE",
            change_type="MIXED",
            change_reason="Added commercial lending context",
            change_summary={
                "product_lines_added": 1,
                "functions_added": 3,
                "subfunctions_added": 12,
                "activated": 3,
                "deactivated": 1,
                "aliases_added": 2,
            },
            parent_customer_ontology_id=parent_id,
            created_by="user-123",
        )
    )

    repository = make_repository(
        customer=customer,
        association=association,
        cursor=cursor,
    )

    result = repository.create_customer_ontology(
        customer_id="CUST-001",
        ontology_id="banking",
        ontology_version="5.0.1",
        definition={"ProductLine": []},
        customer_ontology_version=2,
        change_type="MIXED",
        change_reason="Added commercial lending context",
        change_summary={
            "product_lines_added": 1,
            "functions_added": 3,
            "subfunctions_added": 12,
            "activated": 3,
            "deactivated": 1,
            "aliases_added": 2,
        },
        parent_customer_ontology_id=parent_id,
        created_by="user-123",
    )

    assert result.customer_ontology_version == 2
    assert result.change_type == "MIXED"
    assert result.change_reason == "Added commercial lending context"
    assert result.change_summary["product_lines_added"] == 1
    assert result.parent_customer_ontology_id == parent_id
    assert result.created_by == "user-123"

    _, params = cursor.executed[0]

    assert params[8] == "MIXED"
    assert params[9] == "Added commercial lending context"
    assert params[11] == parent_id
    assert params[12] == "user-123"


def test_create_customer_ontology_requires_active_industry_allocation():
    customer = SimpleNamespace(
        tenant_id="tenant-001",
        customer_id="CUST-001",
        customer_name="Acme Bank",
        industry_id="banking",
    )

    repository = make_repository(
        customer=customer,
        association=None,
    )

    with pytest.raises(CustomerOntologyCanonicalNotAllocatedError):
        repository.create_customer_ontology(
            customer_id="CUST-001",
            ontology_id="insurance",
            ontology_version="1.0.0",
            definition={},
        )


def test_create_customer_ontology_rejects_inactive_industry_allocation():
    customer = SimpleNamespace(
        tenant_id="tenant-001",
        customer_id="CUST-001",
        customer_name="Acme Bank",
        industry_id="banking",
    )

    association = SimpleNamespace(
        industry_id="banking",
        ontology_id="banking",
        ontology_version="5.0.1",
        status="INACTIVE",
    )

    repository = make_repository(
        customer=customer,
        association=association,
    )

    with pytest.raises(CustomerOntologyCanonicalNotAllocatedError):
        repository.create_customer_ontology(
            customer_id="CUST-001",
            ontology_id="banking",
            ontology_version="5.0.1",
            definition={},
        )


def test_create_customer_ontology_rejects_invalid_version():
    customer = SimpleNamespace(
        tenant_id="tenant-001",
        customer_id="CUST-001",
        customer_name="Acme Bank",
        industry_id="banking",
    )

    association = SimpleNamespace(
        industry_id="banking",
        ontology_id="banking",
        ontology_version="5.0.1",
        status="ACTIVE",
    )

    repository = make_repository(
        customer=customer,
        association=association,
    )

    with pytest.raises(CustomerOntologyInvalidVersionError):
        repository.create_customer_ontology(
            customer_id="CUST-001",
            ontology_id="banking",
            ontology_version="5.0.1",
            definition={},
            customer_ontology_version=0,
        )


def test_get_active_returns_record():
    customer_ontology_id = UUID(
        "22222222-2222-2222-2222-222222222222"
    )

    cursor = FakeCursor(
        row=make_row(
            customer_ontology_id=customer_ontology_id,
            customer_ontology_version=2,
            definition={"ProductLine": []},
            change_type="CONTEXTUALIZATION",
            change_reason="Added payments context",
            change_summary={"product_lines_added": 1},
            created_by="user-123",
        )
    )

    customer = SimpleNamespace(
        tenant_id="tenant-001",
        customer_id="CUST-001",
        customer_name="Acme Bank",
        industry_id="banking",
    )

    repository = make_repository(
        customer=customer,
        cursor=cursor,
    )

    result = repository.get_active("CUST-001")

    assert result is not None
    assert result.customer_ontology_id == customer_ontology_id
    assert result.customer_ontology_version == 2
    assert result.status == "ACTIVE"
    assert result.definition == {"ProductLine": []}
    assert result.change_type == "CONTEXTUALIZATION"
    assert result.change_reason == "Added payments context"
    assert result.change_summary == {"product_lines_added": 1}
    assert result.created_by == "user-123"


def test_get_active_returns_none_when_missing():
    customer = SimpleNamespace(
        tenant_id="tenant-001",
        customer_id="CUST-001",
        customer_name="Acme Bank",
        industry_id="banking",
    )

    repository = make_repository(
        customer=customer,
        cursor=FakeCursor(row=None),
    )

    assert repository.get_active("CUST-001") is None


def test_list_history_returns_descending_rows():
    first_id = UUID("33333333-3333-3333-3333-333333333333")
    second_id = UUID("44444444-4444-4444-4444-444444444444")

    cursor = FakeCursor(
        rows=[
            make_row(
                customer_ontology_id=second_id,
                customer_ontology_version=2,
                status="ACTIVE",
                change_type="CONTEXTUALIZATION",
                change_reason="Added Payments",
                change_summary={"product_lines_added": 1},
                created_by="user-2",
            ),
            make_row(
                customer_ontology_id=first_id,
                customer_ontology_version=1,
                status="INACTIVE",
                change_type="INITIAL",
                change_summary={},
                created_by="SYSTEM",
            ),
        ]
    )

    repository = make_repository(cursor=cursor)

    results = repository.list_history("CUST-001")

    assert [record.customer_ontology_version for record in results] == [
        2,
        1,
    ]

    assert results[0].change_type == "CONTEXTUALIZATION"
    assert results[0].change_reason == "Added Payments"
    assert results[0].created_by == "user-2"

    assert results[1].change_type == "INITIAL"
    assert results[1].created_by == "SYSTEM"


def test_list_versions_remains_compatible():
    cursor = FakeCursor(
        rows=[
            make_row(
                customer_ontology_id=UUID(
                    "55555555-5555-5555-5555-555555555555"
                ),
                customer_ontology_version=1,
            )
        ]
    )

    repository = make_repository(cursor=cursor)

    results = repository.list_versions("CUST-001")

    assert len(results) == 1
    assert results[0].customer_ontology_version == 1


def test_create_customer_ontology_rejects_missing_customer():
    repository = make_repository(customer=None)

    with pytest.raises(Exception, match="Customer not found"):
        repository.create_customer_ontology(
            customer_id="CUST-MISSING",
            ontology_id="banking",
            ontology_version="5.0.1",
            definition={},
        )

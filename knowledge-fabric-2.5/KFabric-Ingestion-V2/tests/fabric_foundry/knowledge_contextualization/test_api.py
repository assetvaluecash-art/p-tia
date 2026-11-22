from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kbfabric_ingest.fabric_foundry.knowledge_contextualization import api
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.repository import (
    CustomerOntologyRecord,
)


def _record(
    *,
    customer_id: str = "CUST-001",
    version: int = 1,
    status: str = "ACTIVE",
    parent=None,
    change_type: str = "INITIAL",
    created_by: str = "SYSTEM",
) -> CustomerOntologyRecord:
    now = datetime.now(timezone.utc)

    return CustomerOntologyRecord(
        customer_ontology_id=uuid4(),
        tenant_id="tenant-001",
        customer_id=customer_id,
        ontology_id="banking",
        ontology_version="5.0.1",
        customer_ontology_version=version,
        status=status,
        definition={
            "domains": [
                {
                    "id": "consumer_retail",
                    "product_lines": [
                        {
                            "id": "personal_banking",
                            "name": "Personal Banking",
                        }
                    ],
                }
            ]
        },
        change_type=change_type,
        change_reason=None,
        change_summary={
            "description": "Initial customer ontology created from canonical ontology"
        },
        parent_customer_ontology_id=parent,
        created_by=created_by,
        created_at=now,
        confirmed_by=created_by,
        confirmed_at=now,
        updated_at=now,
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api.customer_ontology_router)
    return TestClient(app)


def test_get_current_customer_ontology_returns_system_created_v1(monkeypatch):
    record = _record()

    monkeypatch.setattr(
        api.customer_ontology_repository,
        "get_active",
        lambda customer_id: record,
    )

    response = _client().get("/customers/CUST-001/ontology")

    assert response.status_code == 200

    body = response.json()

    assert body["customer_id"] == "CUST-001"
    assert body["customer_ontology_version"] == 1
    assert body["status"] == "ACTIVE"
    assert body["change_type"] == "INITIAL"
    assert body["created_by"] == "SYSTEM"
    assert body["ontology_id"] == "banking"
    assert body["ontology_version"] == "5.0.1"
    assert body["definition"]["domains"][0]["product_lines"][0]["id"] == (
        "personal_banking"
    )


def test_get_current_customer_ontology_returns_platform_not_found(monkeypatch):
    monkeypatch.setattr(
        api.customer_ontology_repository,
        "get_active",
        lambda customer_id: None,
    )

    response = _client().get("/customers/CUST-UNKNOWN/ontology")

    assert response.status_code == 404

    body = response.json()

    assert body["detail"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["detail"]["details"]["customer_id"] == "CUST-UNKNOWN"


def test_get_customer_ontology_history_returns_single_system_created_v1(monkeypatch):
    record = _record()

    monkeypatch.setattr(
        api.customer_ontology_repository,
        "list_history",
        lambda customer_id: [record],
    )

    response = _client().get("/customers/CUST-001/ontology/versions")

    assert response.status_code == 200

    body = response.json()

    assert body["customer_id"] == "CUST-001"
    assert body["current_customer_ontology_version"] == 1
    assert len(body["versions"]) == 1

    version = body["versions"][0]

    assert version["customer_ontology_version"] == 1
    assert version["status"] == "ACTIVE"
    assert version["change_type"] == "INITIAL"
    assert version["created_by"] == "SYSTEM"


def test_get_customer_ontology_history_returns_newest_first(monkeypatch):
    v1 = _record()
    v2 = _record(
        version=2,
        status="ACTIVE",
        parent=v1.customer_ontology_id,
        change_type="CONTEXTUALIZATION",
        created_by="SME-001",
    )
    v1 = _record(
        version=1,
        status="INACTIVE",
        created_by="SYSTEM",
    )

    v2 = CustomerOntologyRecord(
        **{
            **v2.__dict__,
            "parent_customer_ontology_id": v1.customer_ontology_id,
        }
    )

    monkeypatch.setattr(
        api.customer_ontology_repository,
        "list_history",
        lambda customer_id: [v2, v1],
    )

    response = _client().get("/customers/CUST-001/ontology/versions")

    assert response.status_code == 200

    body = response.json()

    assert body["current_customer_ontology_version"] == 2
    assert [
        version["customer_ontology_version"]
        for version in body["versions"]
    ] == [2, 1]

    assert body["versions"][0]["status"] == "ACTIVE"
    assert body["versions"][1]["status"] == "INACTIVE"


def test_get_customer_ontology_history_returns_not_found_when_no_versions(
    monkeypatch,
):
    monkeypatch.setattr(
        api.customer_ontology_repository,
        "list_history",
        lambda customer_id: [],
    )

    response = _client().get("/customers/CUST-UNKNOWN/ontology/versions")

    assert response.status_code == 404

    body = response.json()

    assert body["detail"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["detail"]["details"]["customer_id"] == "CUST-UNKNOWN"

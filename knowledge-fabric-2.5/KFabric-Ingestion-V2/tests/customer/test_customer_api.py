from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kbfabric_ingest.fabric_foundry.customer.api import customer_router
from kbfabric_ingest.fabric_foundry.customer.context import TenantContext
from kbfabric_ingest.fabric_foundry.customer.repository import (
    CustomerAlreadyExistsError,
    CustomerIndustryNotFoundError,
)
from kbfabric_ingest.fabric_foundry.industry.repository import IndustryRecord


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(customer_router)
    return app


def test_onboard_customer_returns_created_customer(monkeypatch) -> None:
    expected = TenantContext(
        tenant_id="tenant-001",
        customer_id="Pulse AI_Acme Bank_tenant-001",
        customer_name="Acme Bank",
        industry_id="banking",
    )

    class FakeIndustryRepository:
        def get_industry(self, *, industry_id):
            assert industry_id == "banking"
            return IndustryRecord(
                industry_id="banking",
                industry_name="Enterprise Banking",
                description="Banking industry",
                status="ACTIVE",
                metadata={},
            )

    class FakeRepository:
        def __init__(self):
            self.industry_repository = FakeIndustryRepository()

        def create_customer(self, **kwargs):
            assert kwargs == {
                "customer_name": "Acme Bank",
                "industry_id": "banking",
                "created_by": "test-user-001",
            }
            return expected

    monkeypatch.setattr(
        "kbfabric_ingest.fabric_foundry.customer.api.CustomerRepository",
        FakeRepository,
    )

    class FakeContextualizationService:
        def __init__(self, *, customer_repository):
            assert isinstance(customer_repository, FakeRepository)

        def initialize_customer_ontology(self, **kwargs):
            assert kwargs == {
                "customer_id": expected.customer_id,
                "created_by": "test-user-001",
                "change_reason": "Initialized during customer onboarding",
            }

    monkeypatch.setattr(
        "kbfabric_ingest.fabric_foundry.customer.api.KnowledgeContextualizationService",
        FakeContextualizationService,
    )

    client = TestClient(_app())

    response = client.post(
        "/customers",
        headers={"Authorization": "Bearer test-user-001"},
        json={
            "customer_name": "Acme Bank",
            "industry_id": "banking",
        },
    )

    assert response.status_code == 201
    assert response.json() == {
        "tenant_id": "tenant-001",
        "customer_id": "Pulse AI_Acme Bank_tenant-001",
        "customer_name": "Acme Bank",
        "industry_id": "banking",
        "industry_name": "Enterprise Banking",
        "status": "ACTIVE",
    }


def test_onboard_customer_rejects_invalid_request() -> None:
    client = TestClient(_app())

    response = client.post(
        "/customers",
        headers={"Authorization": "Bearer test-user-001"},
        json={
            "customer_name": "",
            "industry_id": "banking",
        },
    )

    assert response.status_code == 422


def test_onboard_customer_returns_conflict_for_duplicate(
    monkeypatch,
) -> None:
    class FakeRepository:
        def __init__(self):
            self.industry_repository = None

        def create_customer(self, **kwargs):
            raise CustomerAlreadyExistsError(
                "Customer already exists: Pulse AI_Acme Bank_tenant-001"
            )

    monkeypatch.setattr(
        "kbfabric_ingest.fabric_foundry.customer.api.CustomerRepository",
        FakeRepository,
    )

    client = TestClient(_app())

    response = client.post(
        "/customers",
        headers={"Authorization": "Bearer test-user-001"},
        json={
            "customer_name": "Acme Bank",
            "industry_id": "banking",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Customer already exists: Pulse AI_Acme Bank_tenant-001"
    )


def test_onboard_customer_rejects_unknown_industry(
    monkeypatch,
) -> None:
    class FakeRepository:
        def __init__(self):
            self.industry_repository = None

        def create_customer(self, **kwargs):
            raise CustomerIndustryNotFoundError(
                "Active industry not found: unknown"
            )

    monkeypatch.setattr(
        "kbfabric_ingest.fabric_foundry.customer.api.CustomerRepository",
        FakeRepository,
    )

    client = TestClient(_app())

    response = client.post(
        "/customers",
        headers={"Authorization": "Bearer test-user-001"},
        json={
            "customer_name": "Acme Bank",
            "industry_id": "unknown",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Active industry not found: unknown"
    )


def test_onboard_customer_returns_500_for_unexpected_error(
    monkeypatch,
) -> None:
    class FakeRepository:
        def __init__(self):
            self.industry_repository = None

        def create_customer(self, **kwargs):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "kbfabric_ingest.fabric_foundry.customer.api.CustomerRepository",
        FakeRepository,
    )

    client = TestClient(_app())

    response = client.post(
        "/customers",
        headers={"Authorization": "Bearer test-user-001"},
        json={
            "customer_name": "Acme Bank",
            "industry_id": "banking",
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Customer onboarding failed"

from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kbfabric_ingest.fabric_foundry.api import fabric_foundry_router
from kbfabric_ingest.fabric_foundry.industry.api import (
    industry_ontology_repository,
    industry_repository,
    ontology_repository,
)
from kbfabric_ingest.fabric_foundry.industry.repository import IndustryRecord
from kbfabric_ingest.fabric_foundry.industry.ontology_repository import (
    IndustryOntologyRecord,
)
from kbfabric_ingest.fabric_foundry.ontology.repository import OntologyRecord


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(fabric_foundry_router)
    return app


def test_list_industries_returns_active_industries() -> None:
    records = [
        IndustryRecord(
            industry_id="banking",
            industry_name="Banking",
            description=None,
            status="ACTIVE",
            metadata={},
        ),
        IndustryRecord(
            industry_id="insurance",
            industry_name="Insurance & Retirement Services",
            description=None,
            status="ACTIVE",
            metadata={},
        ),
    ]

    with patch.object(
        industry_repository,
        "list_industries",
        return_value=records,
    ) as list_industries:
        client = TestClient(_app())

        response = client.get("/fabric-foundry/industries")

    assert response.status_code == 200
    assert response.json() == [
        {
            "industry_id": "banking",
            "industry_name": "Banking",
            "status": "ACTIVE",
        },
        {
            "industry_id": "insurance",
            "industry_name": "Insurance & Retirement Services",
            "status": "ACTIVE",
        },
    ]

    list_industries.assert_called_once_with(active_only=True)


def test_list_banking_ontologies_returns_active_version() -> None:
    industry = IndustryRecord(
        industry_id="banking",
        industry_name="Banking",
        description=None,
        status="ACTIVE",
        metadata={},
    )

    association = IndustryOntologyRecord(
        industry_id="banking",
        ontology_id="banking",
        ontology_version="5.0.1",
        status="ACTIVE",
        is_default=True,
    )

    ontology = OntologyRecord(
        ontology_id="banking",
        ontology_name="Enterprise Banking",
        version="5.0.1",
        status="ACTIVE",
        artifact_path="config/enterprise_banking_ontology.yaml",
        metadata={},
    )

    with (
        patch.object(
            industry_repository,
            "get_industry",
            return_value=industry,
        ) as get_industry,
        patch.object(
            industry_ontology_repository,
            "list_for_industry",
            return_value=[association],
        ) as list_for_industry,
        patch.object(
            ontology_repository,
            "get_ontology",
            return_value=ontology,
        ) as get_ontology,
    ):
        client = TestClient(_app())

        response = client.get(
            "/fabric-foundry/industries/banking/ontologies"
        )

    assert response.status_code == 200
    assert response.json() == {
        "industry_id": "banking",
        "industry_name": "Banking",
        "ontologies": [
            {
                "ontology_id": "banking",
                "ontology_name": "Enterprise Banking",
                "version": "5.0.1",
                "status": "ACTIVE",
            }
        ],
    }

    get_industry.assert_called_once_with(
        industry_id="banking",
    )
    list_for_industry.assert_called_once_with(
        industry_id="banking",
        active_only=True,
    )
    get_ontology.assert_called_once_with(
        ontology_id="banking",
        version="5.0.1",
    )


def test_unknown_industry_returns_404() -> None:
    with patch.object(
        industry_repository,
        "get_industry",
        return_value=None,
    ) as get_industry:
        client = TestClient(_app())

        response = client.get(
            "/fabric-foundry/industries/healthcare/ontologies"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == (
        "Industry not found: healthcare"
    )

    get_industry.assert_called_once_with(
        industry_id="healthcare",
    )

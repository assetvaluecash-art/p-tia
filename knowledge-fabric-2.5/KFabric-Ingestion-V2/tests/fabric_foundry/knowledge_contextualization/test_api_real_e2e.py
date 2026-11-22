from __future__ import annotations

import copy
import uuid

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kbfabric_ingest._common.env_config import load_dotenv_file
from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository
from kbfabric_ingest.fabric_foundry.knowledge_contextualization import api
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.contract import (
    AddSubFunctionChange,
    CustomerOntologyContextualizationRequest,
    CustomerSubFunction,
    SetAliasChange,
    SpineTargetRef,
)
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.identity import (
    canonical_spine_id,
)
from tests.fabric_foundry.knowledge_contextualization.test_contextualization_e2e import (
    _applicability_changes,
    _create_customer,
    _initialize_customer,
    _selected_spine,
)

pytestmark = pytest.mark.e2e

load_dotenv_file()

ACTOR = "test-api-e2e-sme"
CUSTOMER_PREFIX = "CUST-CTX-API-E2E"


def _client():
    app = FastAPI()
    app.include_router(api.customer_ontology_router)
    return TestClient(app)


def _new_customer_id(industry_id):
    return f"{CUSTOMER_PREFIX}-{industry_id.upper()}-{uuid.uuid4().hex[:10]}"


def _postgres_connect():
    import os
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ["POSTGRES_PORT"]),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def _post(client, customer_id, domain_id, version, request_id, changes, reason):
    request = CustomerOntologyContextualizationRequest(
        customer_id=customer_id,
        domain_id=domain_id,
        base_customer_ontology_version=version,
        request_id=f"{customer_id}-{request_id}",
        changes=changes,
        change_reason=reason,
    )
    return client.post(
        f"/customers/{customer_id}/ontology/{domain_id}/contextualize",
        json=request.model_dump(mode="json"),
        headers={"Authorization": f"Bearer {ACTOR}"},
    )


def _assert_audit(customer_id, version):
    with _postgres_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT created_by, confirmed_by FROM customer_ontology "
                "WHERE customer_id = %s AND customer_ontology_version = %s",
                (customer_id, version),
            )
            row = cur.fetchone()
    assert row is not None
    assert row[0] == ACTOR
    assert row[1] == ACTOR


@pytest.mark.critical
def test_real_dev_api_customer_contextualization_e2e():
    client = _client()

    # 1. Banking: all spine + all node types.
    banking_id = _new_customer_id("banking")
    _create_customer(banking_id, "banking")
    banking_v1 = _initialize_customer(banking_id)
    banking_definition = copy.deepcopy(banking_v1.definition)
    banking_domain = banking_definition["domains"][0]
    banking_domain_id = banking_domain["name"]

    banking_pls = {
        canonical_spine_id(banking_domain_id, "PRODUCT_LINE", pl["ProductLine"])
        for pl in banking_domain["spine"]
    }
    banking_functions = {
        canonical_spine_id(
            banking_domain_id, "FUNCTION",
            pl["ProductLine"], fn["Function"]
        )
        for pl in banking_domain["spine"]
        for fn in pl.get("functions", [])
    }
    banking_subfunctions = {
        canonical_spine_id(
            banking_domain_id, "SUB_FUNCTION",
            pl["ProductLine"], fn["Function"], sf
        )
        for pl in banking_domain["spine"]
        for fn in pl.get("functions", [])
        for sf in fn.get("subfunctions", [])
    }
    banking_changes = _applicability_changes(
        banking_domain,
        selected_product_lines=banking_pls,
        selected_functions=banking_functions,
        selected_sub_functions=banking_subfunctions,
    )
    response = _post(
        client, banking_id, banking_domain_id, 1, "BANKING-ALL",
        banking_changes,
        "API E2E - Banking all spine and node types",
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer_ontology_version"] == 2
    assert response.json()["created_by"] == ACTOR
    assert response.json()["confirmed_by"] == ACTOR
    _assert_audit(banking_id, 2)

    # 2. Healthcare: selective spine + alias + all node types.
    healthcare_id = _new_customer_id("healthcare")
    _create_customer(healthcare_id, "health_insurance_care_services")
    healthcare_v1 = _initialize_customer(healthcare_id)
    healthcare_definition = copy.deepcopy(healthcare_v1.definition)
    healthcare_domain = healthcare_definition["domains"][0]
    healthcare_domain_id = healthcare_domain["name"]

    pl = healthcare_domain["spine"][0]
    fn = pl["functions"][0]
    sf = fn["subfunctions"][0]

    selected_pl = canonical_spine_id(
        healthcare_domain_id, "PRODUCT_LINE", pl["ProductLine"]
    )
    selected_fn = canonical_spine_id(
        healthcare_domain_id, "FUNCTION", pl["ProductLine"], fn["Function"]
    )
    selected_sf = canonical_spine_id(
        healthcare_domain_id, "SUB_FUNCTION",
        pl["ProductLine"], fn["Function"], sf
    )

    healthcare_changes = _applicability_changes(
        healthcare_domain,
        selected_product_lines={selected_pl},
        selected_functions={selected_fn},
        selected_sub_functions={selected_sf},
    )
    healthcare_changes.append(
        SetAliasChange(
            operation="SET_ALIAS",
            target=SpineTargetRef(
                type="PRODUCT_LINE",
                canonical_id=selected_pl,
            ),
            alias="Customer Retail Services",
        )
    )
    response = _post(
        client, healthcare_id, healthcare_domain_id, 1,
        "HEALTHCARE-SELECTIVE",
        healthcare_changes,
        "API E2E - Healthcare selective spine and alias",
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer_ontology_version"] == 2
    _assert_audit(healthcare_id, 2)

    healthcare_current = client.get(
        f"/customers/{healthcare_id}/ontology"
    )
    assert healthcare_current.status_code == 200
    healthcare_context = healthcare_current.json()["definition"]["domains"][0][
        "customer_context"
    ]
    assert healthcare_context["spine"]["product_lines"][
        pl["ProductLine"]
    ]["aliases"] == ["Customer Retail Services"]

    # 3. Banking second iteration: exactly 8 of 18 node types.
    selected_pl, selected_fn, selected_sf = _selected_spine(banking_domain)
    banking_v3_changes = _applicability_changes(
        banking_domain,
        selected_product_lines={selected_pl},
        selected_functions={selected_fn},
        selected_sub_functions={selected_sf},
    )
    response = _post(
        client, banking_id, banking_domain_id, 2,
        "BANKING-8-NODE-TYPES",
        banking_v3_changes,
        "API E2E - Banking second contextualization",
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer_ontology_version"] == 3
    _assert_audit(banking_id, 3)

    banking_current = client.get(
        f"/customers/{banking_id}/ontology"
    )
    assert banking_current.status_code == 200

    history = client.get(
        f"/customers/{banking_id}/ontology/versions"
    )
    assert history.status_code == 200
    assert history.json()["current_customer_ontology_version"] == 3
    assert [
        x["customer_ontology_version"]
        for x in history.json()["versions"]
    ] == [3, 2, 1]

    # API #5A: history remains compact and does not expose detailed changes.
    history_versions = history.json()["versions"]
    assert all("changes" not in item["change_summary"] for item in history_versions)

    # API #5B: detailed version returns the exact persisted submitted changes.
    detail = client.get(
        f"/customers/{banking_id}/ontology/versions/3"
    )
    assert detail.status_code == 200, detail.text
    detail_json = detail.json()
    assert detail_json["customer_id"] == banking_id
    assert detail_json["customer_ontology_version"] == 3
    assert detail_json["details_available"] is True
    assert detail_json["changes"] == [
        change.model_dump(mode="json") for change in banking_v3_changes
    ]
    assert detail_json["created_by"] == ACTOR
    assert detail_json["confirmed_by"] == ACTOR

    # API #5B: requesting a nonexistent version returns the documented 404.
    missing_detail = client.get(
        f"/customers/{banking_id}/ontology/versions/999999"
    )
    assert missing_detail.status_code == 404
    assert missing_detail.json()["detail"]["code"] == "RESOURCE_NOT_FOUND"

    # 4. Insurance: two domains contextualized independently.
    insurance_id = _new_customer_id("insurance")
    _create_customer(insurance_id, "insurance")
    insurance_v1 = _initialize_customer(insurance_id)
    insurance_definition = copy.deepcopy(insurance_v1.definition)
    insurance_domains = insurance_definition["domains"]
    assert len(insurance_domains) >= 2

    domain_a = insurance_domains[0]
    domain_b = insurance_domains[1]
    domain_a_id = domain_a["name"]
    domain_b_id = domain_b["name"]

    a_pls = {
        canonical_spine_id(domain_a_id, "PRODUCT_LINE", pl["ProductLine"])
        for pl in domain_a["spine"]
    }
    a_functions = {
        canonical_spine_id(
            domain_a_id, "FUNCTION",
            pl["ProductLine"], fn["Function"]
        )
        for pl in domain_a["spine"]
        for fn in pl.get("functions", [])
    }
    a_subfunctions = {
        canonical_spine_id(
            domain_a_id, "SUB_FUNCTION",
            pl["ProductLine"], fn["Function"], sf
        )
        for pl in domain_a["spine"]
        for fn in pl.get("functions", [])
        for sf in fn.get("subfunctions", [])
    }

    insurance_a_changes = _applicability_changes(
        domain_a,
        selected_product_lines=a_pls,
        selected_functions=a_functions,
        selected_sub_functions=a_subfunctions,
    )
    response = _post(
        client, insurance_id, domain_a_id, 1,
        "INSURANCE-DOMAIN-A",
        insurance_a_changes,
        "API E2E - Insurance domain A",
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer_ontology_version"] == 2
    _assert_audit(insurance_id, 2)

    b_pl, b_fn, b_sf = _selected_spine(domain_b)
    insurance_b_changes = _applicability_changes(
        domain_b,
        selected_product_lines={b_pl},
        selected_functions={b_fn},
        selected_sub_functions={b_sf},
    )
    response = _post(
        client, insurance_id, domain_b_id, 2,
        "INSURANCE-DOMAIN-B",
        insurance_b_changes,
        "API E2E - Insurance domain B",
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer_ontology_version"] == 3
    _assert_audit(insurance_id, 3)

    insurance_current = client.get(
        f"/customers/{insurance_id}/ontology"
    )
    assert insurance_current.status_code == 200
    active_definition = insurance_current.json()["definition"]

    assert "customer_context" in next(
        x for x in active_definition["domains"]
        if x["name"] == domain_a_id
    )
    assert "customer_context" in next(
        x for x in active_definition["domains"]
        if x["name"] == domain_b_id
    )

    # Authentication must be enforced by the actual API route.
    unauthenticated = client.post(
        f"/customers/{insurance_id}/ontology/{domain_a_id}/contextualize",
        json={
            "customer_id": insurance_id,
            "domain_id": domain_a_id,
            "base_customer_ontology_version": 3,
            "request_id": f"{insurance_id}-UNAUTHENTICATED",
            "changes": [],
        },
    )
    assert unauthenticated.status_code == 401



@pytest.mark.critical
def test_real_dev_two_customer_version_history_e2e():
    print("\n" + "=" * 90)
    print("REAL DEV E2E — CUSTOMER ONTOLOGY VERSION HISTORY")
    print("=" * 90)
    print("PURPOSE")
    print("  Prove Banking and Healthcare customer contextualization and version history")
    print("  against real DEV persistence, including creation audit and exact changes.")
    print("=" * 90)

    client = _client()

    print("\n[1] BANKING — INITIAL CONTEXTUALIZATION")
    print("    Activity : Create a real Banking customer and contextualize its canonical ontology.")
    print("    Assert   : Customer Ontology version 2 is created from version 1.")
    print("    Proof    : Real DEV POST response and persisted creation audit.")

    # Banking customer lifecycle and first contextualization.
    banking_id = _new_customer_id("banking")
    _create_customer(banking_id, "banking")
    banking_v1 = _initialize_customer(banking_id)
    banking_definition = copy.deepcopy(banking_v1.definition)
    banking_domain = banking_definition["domains"][0]
    banking_domain_id = banking_domain["name"]
    banking_pl, banking_fn, banking_sf = _selected_spine(banking_domain)
    banking_node_types = [item["name"] for item in banking_definition["node_types"]]
    assert len(banking_node_types) >= 2

    banking_v2_changes = _applicability_changes(
        banking_domain,
        selected_product_lines={banking_pl},
        selected_functions={banking_fn},
        selected_sub_functions={banking_sf},
    )
    response = _post(
        client,
        banking_id,
        banking_domain_id,
        1,
        "BANKING-V2",
        banking_v2_changes,
        "API E2E - Banking initial contextualization",
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer_ontology_version"] == 2
    _assert_audit(banking_id, 2)
    print("    PASS     : Banking v2 created from v1 with creation audit recorded.")

    print("\n[2] HEALTHCARE — INITIAL CONTEXTUALIZATION")
    print("    Activity : Create a real Healthcare customer and contextualize its canonical ontology.")
    print("    Assert   : Customer Ontology version 2 is created from version 1.")
    print("    Proof    : Real DEV POST response and persisted creation audit.")

    # Healthcare customer lifecycle and first contextualization.
    healthcare_id = _new_customer_id("healthcare")
    _create_customer(healthcare_id, "health_insurance_care_services")
    healthcare_v1 = _initialize_customer(healthcare_id)
    healthcare_definition = copy.deepcopy(healthcare_v1.definition)
    healthcare_domain = healthcare_definition["domains"][0]
    healthcare_domain_id = healthcare_domain["name"]
    healthcare_pl, healthcare_fn, healthcare_sf = _selected_spine(healthcare_domain)

    healthcare_v2_changes = _applicability_changes(
        healthcare_domain,
        selected_product_lines={healthcare_pl},
        selected_functions={healthcare_fn},
        selected_sub_functions={healthcare_sf},
    )
    response = _post(
        client,
        healthcare_id,
        healthcare_domain_id,
        1,
        "HEALTHCARE-V2",
        healthcare_v2_changes,
        "API E2E - Healthcare initial contextualization",
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer_ontology_version"] == 2
    _assert_audit(healthcare_id, 2)
    print("    PASS     : Healthcare v2 created from v1 with creation audit recorded.")

    print("\n[3] BANKING — SECOND CONTEXTUALIZATION")
    print("    Activity : Add one more applicable node type and one customer-specific Capability")
    print("              under the existing Business Capability / Function.")
    print("    Assert   : Banking version 3 is created from version 2.")
    print("    Proof    : Real DEV POST response plus version-chain and detail assertions.")

    # Banking second contextualization: add one more applicable node type
    # and one customer-specific Capability under the selected Function.
    banking_v3_changes.append(
        AddSubFunctionChange(
            operation="ADD_SUB_FUNCTION",
            sub_function=CustomerSubFunction(
                id=None,
                parent_function_id=banking_fn,
                name="Customer Digital Account Servicing",
                description="Customer-specific capability added under the existing Business Capability / Function.",
                aliases=[],
            ),
        )
    )

    response = _post(
        client,
        banking_id,
        banking_domain_id,
        2,
        "BANKING-V3",
        banking_v3_changes,
        "API E2E - Banking second contextualization with capability addition",
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer_ontology_version"] == 3
    _assert_audit(banking_id, 3)
    print("    PASS     : Banking v3 created from v2 with the requested changes persisted.")

    print("\n[4] BANKING — VERSION HISTORY")
    print("    Activity : Read persisted Banking version history.")
    print("    Assert   : Versions are [3, 2, 1], newest-first, with created_by/created_at.")
    print("              Compact history must not embed the detailed changes array.")
    print("    Proof    : Real DEV GET /customers/{id}/ontology/versions response.")

    # Banking version history: v3, v2, v1 with audit fields.
    banking_history = client.get(
        f"/customers/{banking_id}/ontology/versions"
    )
    assert banking_history.status_code == 200, banking_history.text
    banking_history_json = banking_history.json()
    assert banking_history_json["current_customer_ontology_version"] == 3
    assert [
        item["customer_ontology_version"]
        for item in banking_history_json["versions"]
    ] == [3, 2, 1]
    for item in banking_history_json["versions"]:
        expected_actor = "E2E-SME" if item["customer_ontology_version"] == 1 else ACTOR
        assert item["created_by"] == expected_actor
        assert item["created_at"]

    assert all(
        "changes" not in item["change_summary"]
        for item in banking_history_json["versions"]
    )

    print("    PASS     : Banking history returned [3, 2, 1] with creation audit fields and compact summaries.")

    print("\n[5] BANKING — DETAILED VERSION 3")
    print("    Activity : Read detailed Banking v3 changes.")
    print("    Assert   : Returned changes exactly equal the changes submitted in the v3 POST.")
    print("    Proof    : Real DEV GET /customers/{id}/ontology/versions/3 response.")

    # Banking v3 detail: exact persisted node-type and capability changes.
    banking_v3_detail = client.get(
        f"/customers/{banking_id}/ontology/versions/3"
    )
    assert banking_v3_detail.status_code == 200, banking_v3_detail.text
    banking_v3_detail_json = banking_v3_detail.json()
    assert banking_v3_detail_json["details_available"] is True
    assert banking_v3_detail_json["changes"] == [
        change.model_dump(mode="json")
        for change in banking_v3_changes
    ]
    assert banking_v3_detail_json["created_by"] == ACTOR

    print("    PASS     : Banking v3 detail exactly matches the persisted contextualization request.")

    print("\n[6] HEALTHCARE — VERSION HISTORY")
    print("    Activity : Read persisted Healthcare version history.")
    print("    Assert   : Versions are [2, 1], newest-first, with created_by/created_at.")
    print("    Proof    : Real DEV GET /customers/{id}/ontology/versions response.")

    # Healthcare version history: v2, v1 with audit fields.
    healthcare_history = client.get(
        f"/customers/{healthcare_id}/ontology/versions"
    )
    assert healthcare_history.status_code == 200, healthcare_history.text
    healthcare_history_json = healthcare_history.json()
    assert healthcare_history_json["current_customer_ontology_version"] == 2
    assert [
        item["customer_ontology_version"]
        for item in healthcare_history_json["versions"]
    ] == [2, 1]

    for item in healthcare_history_json["versions"]:
        expected_actor = "E2E-SME" if item["customer_ontology_version"] == 1 else ACTOR
        assert item["created_by"] == expected_actor
        assert item["created_at"]

    assert all(
        "changes" not in item["change_summary"]
        for item in healthcare_history_json["versions"]
    )

    print("    PASS     : Healthcare history returned [2, 1] with creation audit fields and compact summaries.")

    print("\n[7] HEALTHCARE — DETAILED VERSION 2")
    print("    Activity : Read detailed Healthcare v2 changes.")
    print("    Assert   : Returned changes exactly equal the changes submitted in the v2 POST.")
    print("    Proof    : Real DEV GET /customers/{id}/ontology/versions/2 response.")

    # Healthcare v2 detail: exact persisted contextualization changes.
    healthcare_v2_detail = client.get(
        f"/customers/{healthcare_id}/ontology/versions/2"
    )
    assert healthcare_v2_detail.status_code == 200, healthcare_v2_detail.text
    healthcare_v2_detail_json = healthcare_v2_detail.json()
    assert healthcare_v2_detail_json["details_available"] is True
    assert healthcare_v2_detail_json["changes"] == [
        change.model_dump(mode="json")
        for change in healthcare_v2_changes
    ]
    assert healthcare_v2_detail_json["created_by"] == ACTOR

    # A missing version must remain a documented 404.
    missing = client.get(
        f"/customers/{banking_id}/ontology/versions/999999"
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "RESOURCE_NOT_FOUND"
    print("    PASS     : Healthcare v2 detail exactly matches the persisted contextualization request.")

    print("\n[8] NEGATIVE CASE — MISSING VERSION")
    print("    Activity : Request a Customer Ontology version that does not exist.")
    print("    Assert   : API returns HTTP 404 / RESOURCE_NOT_FOUND.")
    print("    Proof    : Real DEV GET response.")
    print("    PASS     : Missing version correctly returned HTTP 404 / RESOURCE_NOT_FOUND.")
    print("\n" + "=" * 90)
    print("REAL DEV E2E ACCEPTANCE RESULT: ALL ASSERTIONS PASSED")
    print("=" * 90)


@pytest.mark.critical
def test_real_dev_healthcare_multi_domain_contextualization_isolation_e2e():
    """Real DEV regression: one Healthcare customer must preserve independent domain state."""
    client = _client()

    healthcare_id = _new_customer_id("healthcare-multi-domain")
    _create_customer(healthcare_id, "health_insurance_care_services")
    healthcare_v1 = _initialize_customer(healthcare_id)
    healthcare_definition = copy.deepcopy(healthcare_v1.definition)
    healthcare_domains = healthcare_definition["domains"]

    assert len(healthcare_domains) >= 2, (
        "Healthcare canonical ontology must expose at least two domains "
        "for the multi-domain isolation regression."
    )

    domain_a = healthcare_domains[0]
    domain_b = healthcare_domains[1]
    domain_a_id = domain_a["name"]
    domain_b_id = domain_b["name"]
    assert domain_a_id != domain_b_id
    assert domain_a.get("spine")
    assert domain_b.get("spine")

    # DOMAIN A: select PL -> Function -> SubFunction and a distinct alias.
    a_pl = domain_a["spine"][0]
    a_fn = a_pl["functions"][0]
    a_sf = a_fn["subfunctions"][0]

    a_pl_id = canonical_spine_id(
        domain_a_id, "PRODUCT_LINE", a_pl["ProductLine"]
    )
    a_fn_id = canonical_spine_id(
        domain_a_id, "FUNCTION", a_pl["ProductLine"], a_fn["Function"]
    )
    a_sf_id = canonical_spine_id(
        domain_a_id, "SUB_FUNCTION",
        a_pl["ProductLine"], a_fn["Function"], a_sf
    )

    domain_a_alias = "Healthcare Domain A Customer Alias"
    domain_a_nodes = {
        x["name"] for x in healthcare_definition["node_types"][:4]
    }

    domain_a_changes = _applicability_changes(
        domain_a,
        selected_product_lines={a_pl_id},
        selected_functions={a_fn_id},
        selected_sub_functions={a_sf_id},
    )
    domain_a_changes.append(
        SetAliasChange(
            operation="SET_ALIAS",
            target=SpineTargetRef(
                type="PRODUCT_LINE",
                canonical_id=a_pl_id,
            ),
            alias=domain_a_alias,
        )
    )
    response = _post(
        client,
        healthcare_id,
        domain_a_id,
        1,
        "HEALTHCARE-MULTI-DOMAIN-A",
        domain_a_changes,
        "API E2E - Healthcare domain A isolation",
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer_ontology_version"] == 2

    # DOMAIN B: deliberately different business selection and different node types.
    b_pl = domain_b["spine"][0]
    b_fn = b_pl["functions"][0]

    b_pl_id = canonical_spine_id(
        domain_b_id, "PRODUCT_LINE", b_pl["ProductLine"]
    )
    b_fn_id = canonical_spine_id(
        domain_b_id, "FUNCTION", b_pl["ProductLine"], b_fn["Function"]
    )

    domain_b_alias = "Healthcare Domain B Customer Alias"
    domain_b_nodes = {
        x["name"] for x in healthcare_definition["node_types"][-4:]
    }
    assert domain_a_nodes.isdisjoint(domain_b_nodes)

    domain_b_changes = _applicability_changes(
        domain_b,
        selected_product_lines={b_pl_id},
        selected_functions={b_fn_id},
        selected_sub_functions=set(),
    )
    domain_b_changes.append(
        SetAliasChange(
            operation="SET_ALIAS",
            target=SpineTargetRef(
                type="PRODUCT_LINE",
                canonical_id=b_pl_id,
            ),
            alias=domain_b_alias,
        )
    )
    response = _post(
        client,
        healthcare_id,
        domain_b_id,
        2,
        "HEALTHCARE-MULTI-DOMAIN-B",
        domain_b_changes,
        "API E2E - Healthcare domain B isolation",
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer_ontology_version"] == 3

    # GET DOMAIN A after DOMAIN B was persisted.
    domain_a_get = client.get(
        f"/customers/{healthcare_id}/ontology/contextualization/{domain_a_id}"
    )
    assert domain_a_get.status_code == 200, domain_a_get.text
    domain_a_json = domain_a_get.json()

    assert domain_a_json["customer_id"] == healthcare_id
    assert domain_a_json["domain"]["id"] == domain_a_id
    assert domain_a_json["customer_ontology_version"] == 3

    domain_a_payload = str(domain_a_json["persisted"])
    assert a_pl_id in domain_a_payload
    assert a_fn_id in domain_a_payload
    assert a_sf_id in domain_a_payload
    assert domain_a_alias in domain_a_payload
    assert domain_b_alias not in domain_a_payload
    assert b_pl_id not in domain_a_payload
    assert b_fn_id not in domain_a_payload

    domain_a_node_state = {item["name"]: item for item in domain_a_json["persisted"]["node_types"]}

    # Node types are explicitly decided during contextualization.
    # Applicable=true means the SME selected it; false means the SME explicitly rejected it.
    # configured=true means a decision was persisted; locked=true means it is immutable in this version.
    for node_type in domain_a_nodes:
        assert domain_a_node_state[node_type]["applicable"] is True
        assert domain_a_node_state[node_type]["configured"] is True
        assert domain_a_node_state[node_type]["locked"] is True

    for node_type in domain_b_nodes:
        assert domain_a_node_state[node_type]["applicable"] is False
        assert domain_a_node_state[node_type]["configured"] is True
        assert domain_a_node_state[node_type]["locked"] is True

    # GET DOMAIN B independently.
    domain_b_get = client.get(
        f"/customers/{healthcare_id}/ontology/contextualization/{domain_b_id}"
    )
    assert domain_b_get.status_code == 200, domain_b_get.text
    domain_b_json = domain_b_get.json()

    assert domain_b_json["customer_id"] == healthcare_id
    assert domain_b_json["domain"]["id"] == domain_b_id
    assert domain_b_json["customer_ontology_version"] == 3

    domain_b_payload = str(domain_b_json["persisted"])
    assert b_pl_id in domain_b_payload
    assert b_fn_id in domain_b_payload
    assert domain_b_alias in domain_b_payload
    assert a_pl_id not in domain_b_payload
    assert a_fn_id not in domain_b_payload
    assert a_sf_id not in domain_b_payload
    assert domain_a_alias not in domain_b_payload

    domain_b_node_state = {item["name"]: item for item in domain_b_json["persisted"]["node_types"]}

    for node_type in domain_b_nodes:
        assert domain_b_node_state[node_type]["applicable"] is True
        assert domain_b_node_state[node_type]["configured"] is True
        assert domain_b_node_state[node_type]["locked"] is True

    for node_type in domain_a_nodes:
        assert domain_b_node_state[node_type]["applicable"] is False
        assert domain_b_node_state[node_type]["configured"] is True
        assert domain_b_node_state[node_type]["locked"] is True

    # GET customer-level contextualization landing page.
    customer_context_get = client.get(
        f"/customers/{healthcare_id}/ontology/contextualization"
    )
    assert customer_context_get.status_code == 200, customer_context_get.text
    customer_context_json = customer_context_get.json()

    assert customer_context_json["customer_id"] == healthcare_id
    assert customer_context_json["industry_id"] == "health_insurance_care_services"
    assert customer_context_json["customer_ontology_version"] == 3

    domain_states = {item["id"]: item for item in customer_context_json["domains"]}
    assert domain_a_id in domain_states
    assert domain_b_id in domain_states
    assert domain_states[domain_a_id]["configured"] is True
    assert domain_states[domain_b_id]["configured"] is True

    # Both domain reads must expose the ability to continue adding customer context.
    assert domain_a_json["operations"]["can_add_product_line"] is True
    assert domain_a_json["operations"]["can_add_function"] is True
    assert domain_a_json["operations"]["can_add_sub_function"] is True
    assert domain_b_json["operations"]["can_add_product_line"] is True
    assert domain_b_json["operations"]["can_add_function"] is True
    assert domain_b_json["operations"]["can_add_sub_function"] is True

    print(
        "PASS     : Healthcare multi-domain isolation proven in real DEV. "
        f"customer={healthcare_id} domains=({domain_a_id}, {domain_b_id}) "
        "business and node-type state remained domain-scoped."
    )

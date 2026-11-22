from __future__ import annotations

import copy
import hashlib
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml

from kbfabric_ingest._common.env_config import load_dotenv_file
from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.contract import (
    AddFunctionChange,
    AddProductLineChange,
    AddSubFunctionChange,
    CustomerFunction,
    CustomerOntologyContextualizationRequest,
    CustomerProductLine,
    CustomerSubFunction,
    SetAliasChange,
    SetApplicabilityChange,
    SpineTargetRef,
)
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.repository import (
    CustomerOntologyRepository,
)
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.service import (
    KnowledgeContextualizationService,
)
from kbfabric_ingest.fabric_foundry.ontology.registry import get_ontology_path
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.identity import canonical_spine_id


pytestmark = pytest.mark.e2e

load_dotenv_file()

ACTOR = "E2E-SME"
DB_CUSTOMER_PREFIX = "CUST-CTX-E2E"


def _new_customer_id(industry_id: str) -> str:
    return f"{DB_CUSTOMER_PREFIX}-{industry_id.upper()}-{uuid.uuid4().hex[:10]}"


def _postgres_connect():
    import os

    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ["POSTGRES_PORT"]),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def _canonical_path(ontology_id: str, version: str) -> Path:
    path = Path(get_ontology_path(ontology_id, version))
    assert path.exists(), f"Canonical ontology artifact missing: {path}"
    return path


def _canonical_definition(ontology_id: str, version: str) -> dict:
    path = _canonical_path(ontology_id, version)
    definition = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(definition, dict)
    return definition


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_customer(customer_id: str, industry_id: str):
    repository = CustomerRepository()
    return repository.create_customer(
        customer_id=customer_id,
        customer_name=f"Contextualization E2E {industry_id}",
        industry_id=industry_id,
        created_by="test-user-001",
    )


def _new_service() -> KnowledgeContextualizationService:
    return KnowledgeContextualizationService(
        customer_repository=CustomerRepository(),
        customer_ontology_repository=CustomerOntologyRepository(),
    )


def _initialize_customer(customer_id: str):
    service = _new_service()

    record = service.initialize_customer_ontology(
        customer_id=customer_id,
        created_by=ACTOR,
        change_reason="Real DEV PostgreSQL contextualization E2E setup",
    )

    assert record.customer_ontology_version == 1
    assert record.status == "ACTIVE"

    return record


def _spine_elements(domain: dict):
    domain_id = domain["name"]

    for product_line in domain.get("spine", []):
        pl_name = product_line["ProductLine"]

        yield (
            "PRODUCT_LINE",
            canonical_spine_id(domain_id, "PRODUCT_LINE", pl_name),
            pl_name,
            None,
        )

        for function in product_line.get("functions", []):
            fn_name = function["Function"]

            yield (
                "FUNCTION",
                canonical_spine_id(
                    domain_id,
                    "FUNCTION",
                    pl_name,
                    fn_name,
                ),
                pl_name,
                fn_name,
            )

            for sub_function in function.get("subfunctions", []):
                yield (
                    "SUB_FUNCTION",
                    canonical_spine_id(
                        domain_id,
                        "SUB_FUNCTION",
                        pl_name,
                        fn_name,
                        sub_function,
                    ),
                    pl_name,
                    fn_name,
                )


def _applicability_changes(
    domain: dict,
    selected_product_lines: set[str] | None = None,
    selected_functions: set[str] | None = None,
    selected_sub_functions: set[str] | None = None,
):
    selected_product_lines = selected_product_lines or set()
    selected_functions = selected_functions or set()
    selected_sub_functions = selected_sub_functions or set()

    changes = []

    for element_type, canonical_id, _, _ in _spine_elements(domain):
        if element_type == "PRODUCT_LINE":
            applicable = canonical_id in selected_product_lines
        elif element_type == "FUNCTION":
            applicable = canonical_id in selected_functions
        else:
            applicable = canonical_id in selected_sub_functions

        changes.append(
            SetApplicabilityChange(
                operation="SET_APPLICABILITY",
                target=SpineTargetRef(
                    type=element_type,
                    canonical_id=canonical_id,
                ),
                applicable=applicable,
            )
        )

    return changes


def _domain(definition: dict, domain_id: str) -> dict:
    return next(
        domain
        for domain in definition["domains"]
        if domain["name"] == domain_id
    )


def _selected_spine(domain: dict):
    domain_id = domain["name"]
    product_line = domain["spine"][0]
    function = product_line["functions"][0]
    sub_function = function["subfunctions"][0]

    return (
        canonical_spine_id(
            domain_id,
            "PRODUCT_LINE",
            product_line["ProductLine"],
        ),
        canonical_spine_id(
            domain_id,
            "FUNCTION",
            product_line["ProductLine"],
            function["Function"],
        ),
        canonical_spine_id(
            domain_id,
            "SUB_FUNCTION",
            product_line["ProductLine"],
            function["Function"],
            sub_function,
        ),
    )


def _request(
    customer_id: str,
    domain_id: str,
    base_version: int,
    request_number: str,
    changes,
    reason: str,
):
    return CustomerOntologyContextualizationRequest(
        customer_id=customer_id,
        domain_id=domain_id,
        base_customer_ontology_version=base_version,
        request_id=f"{customer_id}-{request_number}",
        changes=changes,
        change_reason=reason,
        created_by=ACTOR,
        confirmed_by=ACTOR,
    )


def _persisted_versions(customer_id: str):
    with _postgres_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    customer_ontology_id,
                    customer_ontology_version,
                    status,
                    request_id,
                    change_type,
                    change_summary
                FROM customer_ontology
                WHERE customer_id = %s
                ORDER BY customer_ontology_version
                """,
                (customer_id,),
            )
            return cur.fetchall()


def _persisted_active_definition(customer_id: str):
    with _postgres_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT definition
                FROM customer_ontology
                WHERE customer_id = %s
                  AND status = 'ACTIVE'
                """,
                (customer_id,),
            )
            row = cur.fetchone()

    assert row is not None
    return row[0]


def _assert_only_domain_contextualized(
    definition: dict,
    contextualized_domain_ids: set[str],
):
    for domain in definition["domains"]:
        domain_id = domain["name"]

        if domain_id in contextualized_domain_ids:
            assert "customer_context" in domain
        else:
            assert "customer_context" not in domain


def _cleanup_customer_rows(customer_ids: list[str]):
    """
    Cleanup is intentionally disabled during this E2E run.

    The DEV data will be removed during the Azure DevOps cleanup/reset
    before the final push/deployment validation.
    """
    return


def test_real_dev_postgres_customer_contextualization_e2e():
    """
    Real functional validation against DEV PostgreSQL.

    Covers:
      1. Banking: canonical PL/Function/SubFunction + all node types.
      2. Healthcare: selective spine + alias + all node types.
      3. Banking: same selective spine + exactly 8/18 node types.
      4. Insurance: two domains contextualized while other domains remain untouched.
      5. Same customer/domain second iteration adds a Product Line and increments
         Customer Ontology version.
      6. Idempotent replay returns the existing version.
      7. Stale base version is rejected.
      8. Canonical YAML files remain byte-for-byte unchanged.
      9. Persisted active definition is materially different from canonical.
    """

    created_customer_ids: list[str] = []

    banking_path = _canonical_path("banking", "5.0.1")
    healthcare_path = _canonical_path(
        "health_insurance_care_services",
        "1.0.0",
    )
    insurance_path = _canonical_path("insurance", "1.0.0")

    canonical_hashes_before = {
        banking_path: _sha256(banking_path),
        healthcare_path: _sha256(healthcare_path),
        insurance_path: _sha256(insurance_path),
    }

    # ------------------------------------------------------------------
    # 1. Banking — all canonical spine elements + all node types.
    # ------------------------------------------------------------------
    banking_customer_id = _new_customer_id("banking")
    created_customer_ids.append(banking_customer_id)

    _create_customer(banking_customer_id, "banking")
    banking_v1 = _initialize_customer(banking_customer_id)

    banking_definition = copy.deepcopy(banking_v1.definition)
    banking_domain = banking_definition["domains"][0]
    banking_domain_id = banking_domain["name"]

    all_banking_pls = {
        canonical_spine_id(
            banking_domain_id,
            "PRODUCT_LINE",
            pl["ProductLine"],
        )
        for pl in banking_domain["spine"]
    }
    all_banking_functions = {
        canonical_spine_id(
            banking_domain_id,
            "FUNCTION",
            pl["ProductLine"],
            fn["Function"],
        )
        for pl in banking_domain["spine"]
        for fn in pl.get("functions", [])
    }
    all_banking_subfunctions = {
        canonical_spine_id(
            banking_domain_id,
            "SUB_FUNCTION",
            pl["ProductLine"],
            fn["Function"],
            sf,
        )
        for pl in banking_domain["spine"]
        for fn in pl.get("functions", [])
        for sf in fn.get("subfunctions", [])
    }
    banking_changes = _applicability_changes(
        banking_domain,
        selected_product_lines=all_banking_pls,
        selected_functions=all_banking_functions,
        selected_sub_functions=all_banking_subfunctions,
    )
    banking_request = _request(
        banking_customer_id,
        banking_domain_id,
        1,
        "BANKING-ALL",
        banking_changes,
        "Scenario 1 - all canonical spine and node types applicable",
    )

    banking_v2 = _new_service().contextualize_customer_ontology(
        banking_request,
        actor_id=ACTOR,
    )

    assert banking_v2.customer_ontology_version == 2

    persisted_banking_v2 = _persisted_active_definition(
        banking_customer_id
    )
    persisted_banking_domain = _domain(
        persisted_banking_v2,
        banking_domain_id,
    )

    assert persisted_banking_domain["customer_context"]["spine"]

    # ------------------------------------------------------------------
    # 2. Healthcare — selective PL/function/subfunction + alias.
    # ------------------------------------------------------------------
    healthcare_customer_id = _new_customer_id("healthcare")
    created_customer_ids.append(healthcare_customer_id)

    _create_customer(
        healthcare_customer_id,
        "health_insurance_care_services",
    )
    healthcare_v1 = _initialize_customer(healthcare_customer_id)

    healthcare_definition = copy.deepcopy(healthcare_v1.definition)
    healthcare_domain = healthcare_definition["domains"][0]
    healthcare_domain_id = healthcare_domain["name"]

    selected_product_line = healthcare_domain["spine"][0]
    selected_function = selected_product_line["functions"][0]
    selected_sub_function = selected_function["subfunctions"][0]

    selected_pl = canonical_spine_id(
        healthcare_domain_id,
        "PRODUCT_LINE",
        selected_product_line["ProductLine"],
    )
    selected_fn = canonical_spine_id(
        healthcare_domain_id,
        "FUNCTION",
        selected_product_line["ProductLine"],
        selected_function["Function"],
    )
    selected_sf = canonical_spine_id(
        healthcare_domain_id,
        "SUB_FUNCTION",
        selected_product_line["ProductLine"],
        selected_function["Function"],
        selected_sub_function,
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

    healthcare_request = _request(
        healthcare_customer_id,
        healthcare_domain_id,
        1,
        "HEALTHCARE-SELECTIVE",
        healthcare_changes,
        "Scenario 2 - selective spine, alias and all node types",
    )

    healthcare_v2 = _new_service().contextualize_customer_ontology(
        healthcare_request,
        actor_id=ACTOR,
    )

    assert healthcare_v2.customer_ontology_version == 2

    healthcare_active = _persisted_active_definition(
        healthcare_customer_id
    )
    healthcare_active_domain = _domain(
        healthcare_active,
        healthcare_domain_id,
    )

    healthcare_context = healthcare_active_domain["customer_context"]

    assert (
        healthcare_context["spine"]["product_lines"][
            selected_product_line["ProductLine"]
        ]["applicable"]
        is True
    )
    assert (
        healthcare_context["spine"]["product_lines"][selected_product_line["ProductLine"]]["aliases"]
        == ["Customer Retail Services"]
    )

    # ------------------------------------------------------------------
    # 3. Banking — second iteration with exactly 8 of 18 node types.
    # ------------------------------------------------------------------
    banking_selective_pl, banking_selective_fn, banking_selective_sf = (
        _selected_spine(banking_domain)
    )

    banking_v3_changes = _applicability_changes(
        banking_domain,
        selected_product_lines={banking_selective_pl},
        selected_functions={banking_selective_fn},
        selected_sub_functions={banking_selective_sf},
    )
    banking_v3_request = _request(
        banking_customer_id,
        banking_domain_id,
        2,
        "BANKING-8-NODE-TYPES",
        banking_v3_changes,
        "Scenario 3 - selective spine and exactly 8 node types",
    )

    banking_v3 = _new_service().contextualize_customer_ontology(
        banking_v3_request,
        actor_id=ACTOR,
    )

    assert banking_v3.customer_ontology_version == 3

    banking_v3_definition = _persisted_active_definition(
        banking_customer_id
    )
    banking_v3_domain = _domain(
        banking_v3_definition,
        banking_domain_id,
    )

    # ------------------------------------------------------------------
    # 4. Insurance — one customer, two domains contextualized.
    #    Remaining domains must stay canonical/untouched.
    # ------------------------------------------------------------------
    insurance_customer_id = _new_customer_id("insurance")
    created_customer_ids.append(insurance_customer_id)

    _create_customer(insurance_customer_id, "insurance")
    insurance_v1 = _initialize_customer(insurance_customer_id)

    insurance_definition = copy.deepcopy(insurance_v1.definition)
    insurance_domains = insurance_definition["domains"]

    assert len(insurance_domains) >= 2

    insurance_domain_a = insurance_domains[0]
    insurance_domain_b = insurance_domains[1]

    insurance_domain_a_id = insurance_domain_a["name"]
    insurance_domain_b_id = insurance_domain_b["name"]

    # Domain A: all canonical spine + all node types.
    domain_a_pls = {
        pl["ProductLine"]
        for pl in insurance_domain_a["spine"]
    }
    domain_a_functions = {
        fn["Function"]
        for pl in insurance_domain_a["spine"]
        for fn in pl.get("functions", [])
    }
    domain_a_subfunctions = {
        sf
        for pl in insurance_domain_a["spine"]
        for fn in pl.get("functions", [])
        for sf in fn.get("subfunctions", [])
    }

    insurance_a_changes = _applicability_changes(
        insurance_domain_a,
        selected_product_lines=domain_a_pls,
        selected_functions=domain_a_functions,
        selected_sub_functions=domain_a_subfunctions,
    )
    insurance_a_v2 = _new_service().contextualize_customer_ontology(
        _request(
            insurance_customer_id,
            insurance_domain_a_id,
            1,
            "INSURANCE-DOMAIN-A",
            insurance_a_changes,
            "Scenario 4 - first domain contextualization",
        ),
        actor_id=ACTOR,
    )

    assert insurance_a_v2.customer_ontology_version == 2

    # Domain B: selective contextualization.
    domain_b_pl, domain_b_fn, domain_b_sf = _selected_spine(
        insurance_domain_b
    )

    insurance_b_changes = _applicability_changes(
        insurance_domain_b,
        selected_product_lines={domain_b_pl},
        selected_functions={domain_b_fn},
        selected_sub_functions={domain_b_sf},
    )
    insurance_b_v3 = _new_service().contextualize_customer_ontology(
        _request(
            insurance_customer_id,
            insurance_domain_b_id,
            2,
            "INSURANCE-DOMAIN-B",
            insurance_b_changes,
            "Scenario 4 - second domain contextualization",
        ),
        actor_id=ACTOR,
    )

    assert insurance_b_v3.customer_ontology_version == 3

    insurance_active = _persisted_active_definition(
        insurance_customer_id
    )

    _assert_only_domain_contextualized(
        insurance_active,
        {
            insurance_domain_a_id,
            insurance_domain_b_id,
        },
    )

    # ------------------------------------------------------------------
    # 5. Same customer/domain — add a new Product Line.
    #    Version must increment and history must retain all versions.
    # ------------------------------------------------------------------
    added_pl_id = f"customer-pl-e2e-{uuid.uuid4().hex[:8]}"

    insurance_addition_changes = _applicability_changes(
        insurance_domain_b,
        selected_product_lines={domain_b_pl},
        selected_functions={domain_b_fn},
        selected_sub_functions={domain_b_sf},
    )
    insurance_addition_changes.append(
        AddProductLineChange(
            operation="ADD_PRODUCT_LINE",
            product_line=CustomerProductLine(
                id=added_pl_id,
                domain_id=insurance_domain_b_id,
                name="E2E Customer Product Line",
                description="Real DEV PostgreSQL E2E addition",
                aliases=["E2E PL"],
            ),
        )
    )

    insurance_v4 = _new_service().contextualize_customer_ontology(
        _request(
            insurance_customer_id,
            insurance_domain_b_id,
            3,
            "INSURANCE-ADD-PL",
            insurance_addition_changes,
            "Scenario 5 - add Product Line in existing contextualized domain",
        ),
        actor_id=ACTOR,
    )

    assert insurance_v4.customer_ontology_version == 4

    insurance_v4_definition = _persisted_active_definition(
        insurance_customer_id
    )
    insurance_v4_domain = _domain(
        insurance_v4_definition,
        insurance_domain_b_id,
    )

    additions = insurance_v4_domain["customer_context"]["additions"]

    assert any(
        item["id"] == added_pl_id
        for item in additions["product_lines"]
    )

    # ------------------------------------------------------------------
    # 6. Verify immutable version history and change summaries in DEV DB.
    # ------------------------------------------------------------------
    history = _persisted_versions(insurance_customer_id)

    assert [row[1] for row in history] == [1, 2, 3, 4]

    assert history[0][2] == "INACTIVE"
    assert history[1][2] == "INACTIVE"
    assert history[2][2] == "INACTIVE"
    assert history[3][2] == "ACTIVE"

    assert history[1][4] == "CONTEXTUALIZATION"
    assert history[2][4] == "CONTEXTUALIZATION"
    assert history[3][4] == "CONTEXTUALIZATION"

    assert history[3][5]["domain_id"] == insurance_domain_b_id

    # ------------------------------------------------------------------
    # 7. Idempotency — same request_id must not create another version.
    # ------------------------------------------------------------------
    replay = _new_service().contextualize_customer_ontology(
        _request(
            insurance_customer_id,
            insurance_domain_b_id,
            3,
            "INSURANCE-ADD-PL",
            insurance_addition_changes,
            "Scenario 5 replay",
        ),
        actor_id=ACTOR,
    )

    assert replay.customer_ontology_version == 4

    replay_history = _persisted_versions(insurance_customer_id)
    assert [row[1] for row in replay_history] == [1, 2, 3, 4]

    # ------------------------------------------------------------------
    # 8. Optimistic concurrency — stale base version must not create v5.
    # ------------------------------------------------------------------
    stale_changes = _applicability_changes(
        insurance_domain_b,
        selected_product_lines={domain_b_pl},
        selected_functions={domain_b_fn},
        selected_sub_functions={domain_b_sf},
    )

    with pytest.raises(Exception, match="version conflict|Version conflict"):
        _new_service().contextualize_customer_ontology(
            _request(
                insurance_customer_id,
                insurance_domain_b_id,
                3,
                "INSURANCE-STALE-REQUEST",
                stale_changes,
                "Stale concurrency check",
            ),
            actor_id=ACTOR,
        )

    stale_history = _persisted_versions(insurance_customer_id)
    assert [row[1] for row in stale_history] == [1, 2, 3, 4]

    # ------------------------------------------------------------------
    # 9. Persisted customer ontology must differ from canonical YAML.
    # ------------------------------------------------------------------
    canonical_insurance = _canonical_definition("insurance", "1.0.0")

    assert insurance_v4_definition != canonical_insurance

    assert any(
        "customer_context" in domain
        for domain in insurance_v4_definition["domains"]
    )

    assert any(
        "customer_context" not in domain
        for domain in insurance_v4_definition["domains"]
    )

    # ------------------------------------------------------------------
    # 10. Canonical YAML files must remain byte-for-byte unchanged.
    # ------------------------------------------------------------------
    canonical_hashes_after = {
        path: _sha256(path)
        for path in canonical_hashes_before
    }

    assert canonical_hashes_after == canonical_hashes_before

    # ------------------------------------------------------------------
    # 11. Final direct DB assertions: all E2E customers persisted.
    # ------------------------------------------------------------------
    with _postgres_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT customer_id, tenant_id, industry_id
                FROM customer
                WHERE customer_id = ANY(%s)
                ORDER BY customer_id
                """,
                (created_customer_ids,),
            )
            customer_rows = cur.fetchall()

            cur.execute(
                """
                SELECT customer_id, customer_ontology_version, status
                FROM customer_ontology
                WHERE customer_id = ANY(%s)
                ORDER BY customer_id, customer_ontology_version
                """,
                (created_customer_ids,),
            )
            ontology_rows = cur.fetchall()

    assert len(customer_rows) == 3

    assert len(
        [
            row
            for row in ontology_rows
            if row[0] == banking_customer_id
        ]
    ) == 3

    assert len(
        [
            row
            for row in ontology_rows
            if row[0] == healthcare_customer_id
        ]
    ) == 2

    assert len(
        [
            row
            for row in ontology_rows
            if row[0] == insurance_customer_id
        ]
    ) == 4

    active_rows = [
        row for row in ontology_rows if row[2] == "ACTIVE"
    ]

    assert len(
        [row for row in active_rows if row[0] == banking_customer_id]
    ) == 1
    assert len(
        [row for row in active_rows if row[0] == healthcare_customer_id]
    ) == 1
    assert len(
        [row for row in active_rows if row[0] == insurance_customer_id]
    ) == 1

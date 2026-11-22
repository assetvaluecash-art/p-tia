"""Real-store E2E for synthetic banking nodes and semantic relationships."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
import requests

from kbfabric_ingest._common.env_config import load_dotenv_file
from kbfabric_ingest.fabric_foundry.customer.repository import CustomerRepository
from kbfabric_ingest.fabric_foundry.runtime.effective_ontology_context import (
    EffectiveDomainOntologyResolver,
)
from kbfabric_ingest.ingest.config import get_store_name_for_domain
from kbfabric_ingest.ingest.ingestion_service import IngestionService
from kbfabric_ingest.ingest.repository.hitl_review_repository import HITLReviewRepository
from kbfabric_ingest.ingest.ingestion_metadata import (
    customer_scope_id,
    document_id_for_source,
    scoped_block_id,
    scoped_entity_id,
)

pytestmark = pytest.mark.e2e
load_dotenv_file()

DOMAIN = "consumer_retail"
PRODUCT_LINE = "Retail Cards"
PRODUCT_LINE_ID = "DOMAIN:consumer_retail|PRODUCT_LINE:Retail Cards"
FIXTURE = Path("tests/ingest/golden_pack/assets/synthetic_banking_relationships.md")


def test_synthetic_banking_relationships_reach_the_graph(tmp_path):
    """Objective: run a synthetic banking document through real extraction,
    mapping, endpoint resolution, planning, and Neo4j persistence. This matters
    because a useful graph needs both correct function/sub-function placement
    and semantic links between extracted concepts. Invariant: the frozen Retail
    Cards ontology governs the run; the Requirement is either queued with its
    proposed context resolution or, when admitted, attaches to a SubFunction;
    Process -> Rule, Rule -> Regulation, and Control -> Rule relationships are
    admitted and present in Neo4j with this run's batch provenance. A queued
    node-admission decision must remain absent from this run's graph writes.
    The test writes to dev stores
    and therefore requires an explicit environment gate.
    """
    if os.getenv("RUN_SYNTHETIC_RELATIONSHIP_E2E") != "1":
        pytest.skip("Set RUN_SYNTHETIC_RELATIONSHIP_E2E=1 to write synthetic graph data")

    assert FIXTURE.is_file(), f"Synthetic fixture missing: {FIXTURE}"
    customer = CustomerRepository().get_active_customer()
    assert customer is not None and customer.industry_id == "banking"
    ontology = EffectiveDomainOntologyResolver().resolve(
        customer_id=customer.customer_id,
        domain_id=DOMAIN,
        product_line_id=PRODUCT_LINE_ID,
        product_line_name=PRODUCT_LINE,
    )

    source_file = f"synthetic_banking_relationships_{uuid.uuid4().hex[:10]}.txt"
    input_dir = tmp_path / "synthetic_relationships"
    input_dir.mkdir()
    (input_dir / source_file).write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    service = IngestionService()
    job_id = f"synthetic-relationships-{uuid.uuid4().hex[:12]}"
    previous_raw_trace = os.environ.get("KBFABRIC_TEST_RAW_TRACE")
    os.environ["KBFABRIC_TEST_RAW_TRACE"] = "1"
    try:
        result = service.run_ingestion(
            input_dir=input_dir,
            domain=DOMAIN,
            product_line_id=PRODUCT_LINE_ID,
            product_line_name=PRODUCT_LINE,
            job_id=job_id,
        )
    finally:
        if previous_raw_trace is None:
            os.environ.pop("KBFABRIC_TEST_RAW_TRACE", None)
        else:
            os.environ["KBFABRIC_TEST_RAW_TRACE"] = previous_raw_trace

    print(
        "[SYNTHETIC-E2E-RESULT] "
        + json.dumps(
            {
                "job_id": job_id,
                "source_file": source_file,
                "domain_id": service._run_context.ontology.domain_id if service._run_context else None,
                "product_line_id": service._run_context.ontology.product_line_id if service._run_context else None,
                "customer_ontology_version": service._run_context.ontology.customer_ontology_version if service._run_context else None,
                "total_blocks": result.get("total_blocks", 0),
                "entities_extracted": result.get("entities_extracted", 0),
                "relationships_extracted": result.get("relationships_extracted", 0),
                "kg_admission": result.get("kg_admission", {}),
                "neo4j_nodes": result.get("neo4j_nodes", 0),
                "neo4j_relationships": result.get("neo4j_relationships", 0),
            },
            default=str,
            sort_keys=True,
        ),
        flush=True,
    )

    hitl_rows = HITLReviewRepository().list_by_ingestion_batch(job_id)
    print(
        "[SYNTHETIC-E2E-HITL-ROWS] "
        + json.dumps(hitl_rows, default=str, sort_keys=True),
        flush=True,
    )
    admission = result.get("kg_admission") or {}
    assert len(hitl_rows) == admission.get("hitl_queued", 0), (
        f"HITL DB rows ({len(hitl_rows)}) do not match queued plans "
        f"({admission.get('hitl_queued', 0)}): {hitl_rows}"
    )
    assert {row["review_id"] for row in hitl_rows} == set(admission.get("hitl_review_ids", []))

    assert not result.get("error"), result
    assert service._run_context is not None
    assert service._run_context.tenant_context.customer_id == customer.customer_id
    assert service._run_context.ontology.customer_ontology_version == ontology.customer_ontology_version
    assert service._run_context.ontology.product_line_id == PRODUCT_LINE_ID
    assert result.get("total_blocks", 0) > 0, (
        "The source fixture must parse into at least one block before IE assertions"
    )
    assert result.get("entities_extracted", 0) >= 4, result
    assert result.get("relationships_extracted", 0) >= 3, result
    admission = result.get("kg_admission") or {}
    assert admission.get("entities_admitted", 0) >= 4, admission
    assert admission.get("relationships_admitted", 0) >= 3, admission
    assert admission.get("hitl_queued", 0) >= 1, admission
    assert any((row.get("execution_plan_json") or {}).get("context_resolutions") for row in hitl_rows), hitl_rows

    with service.kg_writer.driver.session() as session:
        rows = session.run(
            """
            MATCH (source)-[r]->(target)
            WHERE r.domain = $domain AND r.customer_id = $customer_id
              AND $batch_id IN coalesce(r.ingestion_batch_ids, [])
              AND r.source_file = $source_file
              AND type(r) IN ["GOVERNED_BY", "ENFORCED_BY", "MITIGATES", "PERFORMS"]
            RETURN type(r) AS relationship, labels(source)[0] AS source_type,
                   labels(target)[0] AS target_type, count(*) AS count
            """,
            domain=DOMAIN,
            customer_id=customer.customer_id,
            batch_id=job_id,
            source_file=source_file,
        )
        persisted = {
            (row["relationship"], row["source_type"], row["target_type"])
            for row in rows
        }
        assert ("GOVERNED_BY", "Process", "Rule") in persisted, persisted
        assert ("ENFORCED_BY", "Rule", "Regulation") in persisted, persisted
        assert ("MITIGATES", "Control", "Rule") in persisted, persisted
        batch_nodes = list(session.run(
            """
            MATCH (node)
            WHERE node.domain = $domain
              AND $batch_id IN coalesce(node.ingestion_batch_ids, [])
            RETURN labels(node)[0] AS node_type, node.name_display AS name,
                   node.name AS normalized_name, node.entity_id AS entity_id,
                   node.customer_id AS customer_id, node.tenant_id AS tenant_id,
                   node.ingestion_product_line_ids AS product_line_ids,
                   node.project_ids AS project_ids, node.context_tags AS context_tags,
                   node.block_id AS stored_block_id, node.source_block_id AS source_block_id,
                   node.document_id AS document_id, node.source_file AS source_file
            ORDER BY node_type, name
            """,
            domain=DOMAIN,
            batch_id=job_id,
        ))
        batch_relationships = list(session.run(
            """
            MATCH (source)-[relationship]->(target)
            WHERE relationship.domain = $domain
              AND $batch_id IN coalesce(relationship.ingestion_batch_ids, [])
            RETURN type(relationship) AS relationship,
                   labels(source)[0] AS source_type,
                   labels(target)[0] AS target_type,
                   relationship.customer_id AS customer_id,
                   relationship.tenant_id AS tenant_id,
                   relationship.ingestion_product_line_ids AS product_line_ids,
                   relationship.project_ids AS project_ids,
                   relationship.context_tags AS context_tags,
                   relationship.evidence_contexts AS evidence_contexts
            ORDER BY relationship, source_type, target_type
            """,
            domain=DOMAIN,
            batch_id=job_id,
        ))
        print(
            "[SYNTHETIC-E2E-NEO4J-BATCH-PROVENANCE] "
            + json.dumps(
                {
                    "batch_id": job_id,
                    "nodes": [dict(row) for row in batch_nodes],
                    "relationships": [dict(row) for row in batch_relationships],
                },
                default=str,
                sort_keys=True,
            ),
            flush=True,
        )
        assert batch_nodes, f"No Neo4j nodes carry ingestion batch {job_id}"
        for row in batch_nodes:
            assert row["customer_id"] == customer.customer_id, row
            assert row["tenant_id"] == service._run_context.tenant_context.tenant_id, row
            if row["node_type"] == "Block":
                assert row["stored_block_id"] == scoped_block_id(
                    customer_scope_id(customer.customer_id, service._run_context.tenant_context.tenant_id),
                    row["source_block_id"],
                ), row
            elif row["node_type"] == "Document":
                assert row["document_id"] == document_id_for_source(
                    customer_scope_id(customer.customer_id, service._run_context.tenant_context.tenant_id),
                    row["source_file"],
                ), row
            else:
                normalized_name = service.kg_writer._normalize_entity_value(row["name"] or row["normalized_name"] or "")
                assert row["entity_id"] == scoped_entity_id(
                    customer_scope_id(customer.customer_id, service._run_context.tenant_context.tenant_id),
                    DOMAIN,
                    row["node_type"],
                    normalized_name,
                ), row
                assert PRODUCT_LINE_ID in (row["product_line_ids"] or []), row
        batch_relationship_types = {
            (row["relationship"], row["source_type"], row["target_type"])
            for row in batch_relationships
        }
        for row in batch_relationships:
            assert row["customer_id"] == customer.customer_id, row
            assert row["tenant_id"] == service._run_context.tenant_context.tenant_id, row
            assert PRODUCT_LINE_ID in (row["product_line_ids"] or []), row
            assert row["evidence_contexts"], row
        assert {
            ("GOVERNED_BY", "Process", "Rule"),
            ("ENFORCED_BY", "Rule", "Regulation"),
            ("MITIGATES", "Control", "Rule"),
        }.issubset(batch_relationship_types), batch_relationships

        batch_node_identities = {
            (row["node_type"], (row["name"] or "").casefold())
            for row in batch_nodes
        }
        for review in hitl_rows:
            if review.get("admission_behavior") != "WAIT_FOR_NODE_DECISION":
                continue
            plan = review.get("execution_plan_json") or {}
            for operation in plan.get("entity_operations", []):
                if operation.get("operation") == "CREATE":
                    identity = (
                        operation.get("entity_type"),
                        (operation.get("name") or "").casefold(),
                    )
                    assert identity not in batch_node_identities, (
                        "A node queued for HITL was written before a reviewer decision: "
                        f"{identity}; review={review.get('review_id')}"
                    )

    weaviate_class = get_store_name_for_domain(DOMAIN)
    weaviate_objects = []
    after = None
    while True:
        params = {"class": weaviate_class, "limit": 1000}
        if after:
            params["after"] = after
        weaviate_response = requests.get(
            f"{service.weaviate_writer.url.rstrip(chr(47))}/v1/objects",
            params=params,
            timeout=30,
        )
        weaviate_response.raise_for_status()
        page = weaviate_response.json().get("objects", [])
        weaviate_objects.extend(page)
        if len(page) < 1000:
            break
        after = page[-1].get("id")
        assert after, "Weaviate pagination returned a full page without an object ID"
    run_weaviate_rows = [
        obj for obj in weaviate_objects
        if (obj.get("properties") or {}).get("ingestion_batch_id") == job_id
    ]
    print(
        "[SYNTHETIC-E2E-WEAVIATE-BATCH-PROVENANCE] "
        + json.dumps(
            {
                "batch_id": job_id,
                "class": weaviate_class,
                "source_file": source_file,
                "matching_record_count": len(run_weaviate_rows),
                "records": run_weaviate_rows,
            },
            default=str,
            sort_keys=True,
        ),
        flush=True,
    )
    expected_weaviate_records = result.get("total_blocks", 0)
    assert len(run_weaviate_rows) == expected_weaviate_records, (
        f"Expected {expected_weaviate_records} run-scoped Weaviate records; "
        f"found {len(run_weaviate_rows)}: {run_weaviate_rows}"
    )
    stored_object_ids = set()
    for obj in run_weaviate_rows:
        properties = obj.get("properties") or {}
        assert properties.get("customer_id") == customer.customer_id, properties
        assert properties.get("tenant_id") == service._run_context.tenant_context.tenant_id, properties
        assert properties.get("domain_id") == DOMAIN, properties
        assert properties.get("ingestion_product_line_id") == PRODUCT_LINE_ID, properties
        assert properties.get("document_id") == document_id_for_source(
            customer_scope_id(customer.customer_id, service._run_context.tenant_context.tenant_id),
            source_file,
        ), properties
        assert obj.get("id") == scoped_block_id(
            customer_scope_id(customer.customer_id, service._run_context.tenant_context.tenant_id),
            properties["block_id"],
        ), obj
        stored_object_ids.add(obj["id"])
    assert len(stored_object_ids) == expected_weaviate_records, (
        "Two run blocks unexpectedly share a customer-scoped Weaviate object ID"
    )

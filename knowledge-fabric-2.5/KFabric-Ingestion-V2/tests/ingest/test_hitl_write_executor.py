"""Focused tests for the reviewed-plan Neo4j write boundary."""

import pytest

from kbfabric_ingest.ingest.execution_plan import (
    EntityOperation,
    ExecutionAction,
    ExecutionPlan,
    RelationshipOperation,
)
from kbfabric_ingest.ingest.hitl_write_executor import HITLPlanWriter
from kbfabric_ingest.ingest.ingestion_metadata import customer_scope_id, scoped_entity_id


class _Result:
    def __init__(self, query):
        self.query = query

    def single(self):
        if "RETURN n.entity_id" in self.query:
            return {"entity_id": "node-1"}
        return {"relationship_id": "rel-1"}


class _Tx:
    def __init__(self):
        self.calls = []

    def run(self, query, **params):
        self.calls.append((query, params))
        return _Result(query)


class _Session:
    def __init__(self, tx):
        self.tx = tx

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute_write(self, callback):
        return callback(self.tx)


class _Driver:
    def __init__(self):
        self.tx = _Tx()

    def session(self):
        return _Session(self.tx)


@pytest.fixture
def item():
    return {
        "review_id": "review-1",
        "ingestion_batch_id": "batch-1",
        "source_context_json": {"block_id": "block-1", "source_block_ids": ["block-1"]},
        "document_context_json": {"document_id": "doc-1", "source_file": "requirements.docx"},
        "ontology_context_json": {
            "customer_id": "customer-1", "tenant_id": "tenant-1",
            "domain_id": "consumer_retail", "product_line_id": "pl-cards",
            "customer_ontology_id": "ontology-1", "customer_ontology_version": 2,
            "canonical_ontology_version": "5.0.1",
        },
        "mapping_context_json": {
            "incoming_entity": {"node_type": "Process", "name": "Underwriting"},
            "ontology_constraints": {"relationship_types": [], "allowed_relationship_triples": []},
        },
    }


def test_create_node_write_is_scoped_and_idempotent(item):
    """Objective: verify approved node creation carries frozen scope and source lineage.

    This protects customer isolation and evidence provenance at the graph boundary.
    Invariant: the executor uses deterministic identity and persists the frozen
    batch, product line, ontology versions, document, and source block metadata.
    """
    driver = _Driver()
    writer = HITLPlanWriter(driver=driver)
    plan = ExecutionPlan(
        plan_id="plan-1", action=ExecutionAction.QUEUE_FOR_REVIEW,
        mapping_decision="CREATE_NEW", justification="review approved",
        entity_operations=[EntityOperation(
            operation="CREATE", entity_type="Process", temporary_id="temp-1",
            name="Underwriting", properties={"description": "Credit assessment"},
        )],
    )

    result = writer.execute(plan, item)

    assert result == {"nodes": 1, "relationships": 0, "aliases": 0}
    query, params = driver.tx.calls[0]
    assert "MERGE (n:`Process` {entity_id: $entity_id})" in query
    assert params["base_props"]["customer_id"] == "customer-1"
    assert params["base_props"]["ingestion_batch_ids"] == ["batch-1"]
    assert params["base_props"]["source_document_ids"] == ["doc-1"]
    assert params["base_props"]["source_blocks"] == ["block-1"]


def test_hitl_approval_preserves_customer_root_graph_spine(item):
    """An approved node write carries industry metadata under a customer root."""
    item["ontology_context_json"]["industry_id"] = "banking"
    item["document_context_json"]["product_line_name"] = "Retail Cards"
    item["mapping_context_json"]["incoming_entity"]["name"] = "Card Activation"
    driver = _Driver()
    writer = HITLPlanWriter(driver=driver)
    plan = ExecutionPlan(
        plan_id="plan-industry-spine", action=ExecutionAction.QUEUE_FOR_REVIEW,
        mapping_decision="CREATE_NEW", justification="review approved",
        entity_operations=[EntityOperation(
            operation="CREATE", entity_type="Process", temporary_id="temp-1",
            name="Card Activation", properties={"description": "Activate a retail card"},
        )],
    )

    writer.execute(plan, item)

    queries = [query for query, _params in driver.tx.calls]
    assert any("MERGE (customer:CustomerRoot" in query and "MERGE (domain)-[:PART_OF]->(customer)" in query for query in queries)
    assert not any("MERGE (obsolete:IndustryRoot" in query for query in queries)
    assert any("MERGE (pl)-[:PART_OF]->(domain)" in query for query in queries)
    assert any("MERGE (n)-[:PART_OF]->(pl)" in query for query in queries)
    node_write = next(params for query, params in driver.tx.calls if "MERGE (n:`Process`" in query)
    assert node_write["base_props"]["industry_id"] == "banking"


def test_relationship_write_rechecks_allowed_triple_and_candidate(item):
    """Objective: verify triplet writes use the persisted ranked endpoint and ontology triple.

    This protects relationship integrity after a human selects a different endpoint.
    Invariant: only a selected, persisted candidate in an allowed typed triple reaches
    the idempotent Neo4j MERGE.
    """
    driver = _Driver()
    writer = HITLPlanWriter(driver=driver)
    item["mapping_context_json"]["ontology_constraints"] = {
        "relationship_types": ["HAS_SUBFUNCTION"],
        "allowed_relationship_triples": [["Process", "HAS_SUBFUNCTION", "SubFunction"]],
    }
    plan = ExecutionPlan(
        plan_id="plan-2", action=ExecutionAction.QUEUE_FOR_REVIEW,
        mapping_decision="CREATE_NEW", selected_entity_id="process-1",
        justification="review approved",
        context_resolutions=[{
            "direction": "outgoing", "rel_type": "HAS_SUBFUNCTION",
            "source_entity_id": "process-1", "target_type": "SubFunction",
            "target_value": "Credit Limits", "selected_entity_id": "sub-1",
            "candidates": [{"entity_id": "sub-1", "node_type": "SubFunction"}],
        }],
        relationship_operations=[RelationshipOperation(
            operation="CREATE", rel_type="HAS_SUBFUNCTION",
            source_entity_id="process-1", target_entity_id="sub-1",
            target_type="SubFunction", target_value="Credit Limits",
        )],
    )

    result = writer.execute(plan, item)

    assert result["relationships"] == 1
    query, params = driver.tx.calls[0]
    assert "MERGE (s)-[r:`HAS_SUBFUNCTION`]->(t)" in query
    assert params["source_id"] == "process-1"
    assert params["target_id"] == "sub-1"


def test_relationship_write_resolves_new_primary_temporary_endpoint(item):
    """Objective: map a reviewed new node's temporary endpoint to its scoped ID.

    A node-admission HITL plan may contain an auto-resolved edge from the new
    node to an existing candidate. The persisted source_entity_id can still be
    the temporary extraction ID; the writer must translate it after creating
    the node so Neo4j can match the endpoint.
    """
    item["ontology_context_json"]["industry_id"] = "banking"
    item["document_context_json"]["product_line_name"] = "Retail Cards"
    item["mapping_context_json"]["incoming_entity"] = {
        "node_type": "System", "name": "Digital Banking App",
    }
    item["mapping_context_json"]["ontology_constraints"] = {
        "relationship_types": ["INTEGRATES_WITH"],
        "allowed_relationship_triples": [["System", "INTEGRATES_WITH", "System"]],
    }
    temporary_id = "source-temp-1"
    candidate_id = "existing-system-1"
    plan = ExecutionPlan(
        plan_id="plan-temp-endpoint",
        action=ExecutionAction.WRITE_NOW,
        mapping_decision="CREATE_NEW",
        justification="approved source node with scoped endpoint evidence",
        entity_operations=[EntityOperation(
            operation="CREATE", entity_type="System", temporary_id=temporary_id,
            name="Digital Banking App",
        )],
        context_resolutions=[{
            "direction": "outgoing", "rel_type": "INTEGRATES_WITH",
            "source_entity_id": temporary_id, "target_type": "System",
            "target_value": "Card Authorization Service", "decision": "RESOLVED",
            "selected_entity_id": candidate_id,
            "candidates": [{"entity_id": candidate_id, "node_type": "System"}],
        }],
        relationship_operations=[RelationshipOperation(
            operation="CREATE", rel_type="INTEGRATES_WITH",
            source_entity_id=temporary_id, source_temporary_id=temporary_id,
            target_entity_id=candidate_id, target_type="System",
            target_value="Card Authorization Service",
        )],
        execution_metadata={"source_temporary_id": temporary_id},
    )
    driver = _Driver()

    result = HITLPlanWriter(driver=driver).execute(plan, item)

    assert result["relationships"] == 1
    relationship_call = next(
        (query, params) for query, params in driver.tx.calls
        if "MERGE (s)-[r:`INTEGRATES_WITH`]->(t)" in query
    )
    _query, params = relationship_call
    expected_source_id = scoped_entity_id(
        customer_scope_id("customer-1", "tenant-1"),
        "consumer_retail", "System", "digital banking app",
    )
    assert params["source_id"] == expected_source_id
    assert params["source_id"] != temporary_id
    assert params["target_id"] == candidate_id


def test_writer_fails_closed_for_wrong_node_type(item):
    """Objective: reject a reviewed plan whose node type differs from persisted extraction.

    This prevents a reviewer-facing API payload from widening ontology admission.
    Invariant: no database transaction begins for a type-mismatched plan.
    """
    driver = _Driver()
    plan = ExecutionPlan(
        plan_id="plan-3", action=ExecutionAction.QUEUE_FOR_REVIEW,
        mapping_decision="CREATE_NEW", justification="invalid",
        entity_operations=[EntityOperation(operation="CREATE", entity_type="Rule", name="Underwriting")],
    )
    with pytest.raises(ValueError, match="differs from the persisted extracted node type"):
        HITLPlanWriter(driver=driver).execute(plan, item)
    assert driver.tx.calls == []


def test_alias_reuse_writes_customer_domain_alias_and_provenance(item):
    """Objective: verify explicit alias approval updates both the candidate and alias provenance.

    Customer taxonomy additions must be searchable later and auditable to their
    reviewer/source context. Invariant: alias creation is scoped to the reused
    node and writes provenance without replacing its canonical name.
    """
    driver = _Driver()
    item["mapping_context_json"]["candidate_scores"] = [{"candidate": {
        "entity_id": "process-1", "kg_node_type": "Process", "name": "Credit Underwriting",
    }}]
    plan = ExecutionPlan(
        plan_id="plan-alias", action=ExecutionAction.WRITE_NOW,
        mapping_decision="REUSE_EXISTING", selected_entity_id="process-1",
        justification="review approved",
        entity_operations=[EntityOperation(
            operation="REUSE", entity_type="Process", entity_id="process-1",
            name="Short-Term Credit", properties={
                "aliases": ["Short-Term Credit"], "alias_scope": "CUSTOMER_DOMAIN",
            },
        )],
        execution_metadata={"approved_alias_provenance": {
            "review_id": "review-1", "source_document_id": "doc-1", "source_block_id": "block-1",
        }},
    )

    result = HITLPlanWriter(driver=driver).execute(plan, item)

    assert result["aliases"] == 1
    alias_query, alias_params = driver.tx.calls[-1]
    assert "n.aliases = CASE WHEN $alias IN coalesce(n.aliases, [])" in alias_query
    assert "n.alias_provenance_json = $provenance_json" in alias_query
    assert '"review_id": "review-1"' in alias_params["provenance_json"]


def test_incoming_relationship_writes_candidate_to_extracted_node(item):
    """Objective: verify incoming endpoint corrections preserve relationship direction.

    This protects ontology triple semantics when the chosen candidate is the
    relationship source. Invariant: an incoming resolution writes candidate →
    extracted node with the correct endpoint labels and identities.
    """
    driver = _Driver()
    item["mapping_context_json"]["incoming_entity"] = {"node_type": "Rule", "name": "Credit Rule"}
    item["mapping_context_json"]["ontology_constraints"] = {
        "relationship_types": ["GOVERNS"],
        "allowed_relationship_triples": [["Control", "GOVERNS", "Rule"]],
    }
    plan = ExecutionPlan(
        plan_id="plan-incoming", action=ExecutionAction.WRITE_NOW,
        mapping_decision="REUSE_EXISTING", selected_entity_id="rule-1",
        justification="review approved",
        context_resolutions=[{
            "direction": "incoming", "rel_type": "GOVERNS", "source_entity_id": "rule-1",
            "target_type": "Control", "target_value": "Credit Control",
            "selected_entity_id": "control-1",
            "candidates": [{"entity_id": "control-1", "node_type": "Control"}],
        }],
        relationship_operations=[RelationshipOperation(
            operation="CREATE", rel_type="GOVERNS", source_entity_id="rule-1",
            target_entity_id="control-1", target_type="Control", target_value="Credit Control",
        )],
    )

    HITLPlanWriter(driver=driver).execute(plan, item)

    query, params = driver.tx.calls[0]
    assert "MATCH (s:`Control` {entity_id: $source_id}), (t:`Rule` {entity_id: $target_id})" in query
    assert params["source_id"] == "control-1"
    assert params["target_id"] == "rule-1"

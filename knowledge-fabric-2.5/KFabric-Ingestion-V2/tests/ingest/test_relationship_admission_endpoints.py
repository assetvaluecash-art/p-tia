"""Unit tests for batch and graph endpoint admission."""

from kbfabric_ingest.ingest.ingestion_service import _resolve_relationship_admission
from kbfabric_ingest.ingest.relationship_endpoint_resolution import ContextResolutionResult


def _resolution(*, direction, node_type, node_value, entity_id, decision="RESOLVED"):
    return ContextResolutionResult(
        direction=direction,
        rel_type="GOVERNED_BY",
        source_entity_id="source-entity",
        target_type=node_type,
        target_value=node_value,
        selected_entity_id=entity_id,
        decision=decision,
        reason_code="test_resolution",
    )


def test_relationship_admits_batch_nodes_and_a_persisted_neo4j_target():
    """Objective: verify a document-supported edge can join a newly admitted
    source to an ontology-resolved Neo4j target. This matters because useful
    ingestion must connect new evidence to the existing knowledge graph.
    Invariant: persisted endpoint identity is carried to the writer, and each
    endpoint must be admitted in-batch or resolved to a persisted graph node.
    """
    relationship = {
        "from_type": "Process",
        "from_value": "Credit Limit Review",
        "rel_type": "GOVERNED_BY",
        "to_type": "Rule",
        "to_value": "RULE-SYN-001",
    }
    reason, entity_ids, resolutions = _resolve_relationship_admission(
        relationship,
        batch_admitted_keys={"process:credit limit review"},
        context_resolutions=[_resolution(
            direction="outgoing",
            node_type="Rule",
            node_value="RULE-SYN-001",
            entity_id="neo4j-rule-17",
        )],
    )

    assert reason is None
    assert entity_ids == {"to_entity_id": "neo4j-rule-17"}
    assert resolutions[0]["endpoint"] == "to"


def test_relationship_admits_persisted_neo4j_source_and_batch_target():
    """Objective: verify incoming references can resolve the relationship
    source against Neo4j. This matters because extraction direction must not
    decide whether a valid cross-document graph link can be persisted.
    Invariant: incoming endpoint IDs are preserved for the graph writer.
    """
    relationship = {
        "from_type": "Rule",
        "from_value": "RULE-SYN-001",
        "rel_type": "GOVERNED_BY",
        "to_type": "Process",
        "to_value": "Credit Limit Review",
    }
    reason, entity_ids, _ = _resolve_relationship_admission(
        relationship,
        batch_admitted_keys={"process:credit limit review"},
        context_resolutions=[_resolution(
            direction="incoming",
            node_type="Rule",
            node_value="RULE-SYN-001",
            entity_id="neo4j-rule-17",
        )],
    )

    assert reason is None
    assert entity_ids == {"from_entity_id": "neo4j-rule-17"}


def test_unresolved_graph_endpoint_is_withheld():
    """Objective: ensure an absent endpoint is never created from an unresolved
    reference. This matters for graph integrity and safe automated admission.
    Invariant: an endpoint outside the batch requires a resolved Neo4j ID.
    """
    relationship = {
        "from_type": "Process",
        "from_value": "Credit Limit Review",
        "rel_type": "GOVERNED_BY",
        "to_type": "Rule",
        "to_value": "Unknown Rule",
    }
    reason, entity_ids, _ = _resolve_relationship_admission(
        relationship,
        batch_admitted_keys={"process:credit limit review"},
        context_resolutions=[_resolution(
            direction="outgoing",
            node_type="Rule",
            node_value="Unknown Rule",
            entity_id=None,
            decision="UNRESOLVED_REFERENCE",
        )],
    )

    assert reason == "context_resolution_not_resolved"
    assert entity_ids == {}


def test_relationship_can_use_resolved_graph_endpoints_in_both_directions():
    """Objective: verify both endpoints can resolve to existing Neo4j nodes
    when the document supplies the relationship evidence. This matters for
    attaching a new documented fact to already persisted graph entities.
    Invariant: each absent batch endpoint has its own resolved persisted ID.
    """
    relationship = {
        "from_type": "Rule",
        "from_value": "RULE-SYN-001",
        "rel_type": "GOVERNED_BY",
        "to_type": "Regulation",
        "to_value": "REG-SYN-001",
    }
    reason, entity_ids, _ = _resolve_relationship_admission(
        relationship,
        batch_admitted_keys=set(),
        context_resolutions=[
            _resolution(
                direction="incoming",
                node_type="Rule",
                node_value="RULE-SYN-001",
                entity_id="neo4j-rule-17",
            ),
            _resolution(
                direction="outgoing",
                node_type="Regulation",
                node_value="REG-SYN-001",
                entity_id="neo4j-regulation-23",
            ),
        ],
    )

    assert reason is None
    assert entity_ids == {
        "from_entity_id": "neo4j-rule-17",
        "to_entity_id": "neo4j-regulation-23",
    }


def test_conflicting_resolved_ids_for_one_endpoint_are_withheld():
    """Objective: reject contradictory resolver selections for one endpoint.
    This matters because inconsistent graph identities could create duplicate
    or misdirected edges. Invariant: one endpoint has at most one resolved ID.
    """
    relationship = {
        "from_type": "Process",
        "from_value": "Credit Limit Review",
        "rel_type": "GOVERNED_BY",
        "to_type": "Rule",
        "to_value": "RULE-SYN-001",
    }
    reason, entity_ids, _ = _resolve_relationship_admission(
        relationship,
        batch_admitted_keys={"process:credit limit review"},
        context_resolutions=[
            _resolution(
                direction="outgoing",
                node_type="Rule",
                node_value="RULE-SYN-001",
                entity_id="neo4j-rule-17",
            ),
            _resolution(
                direction="outgoing",
                node_type="Rule",
                node_value="RULE-SYN-001",
                entity_id="neo4j-rule-99",
            ),
        ],
    )

    assert reason == "context_resolution_not_resolved"
    assert entity_ids == {}

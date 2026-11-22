"""Objective: verify relationship endpoint checks preserve ontology edge direction.

Incoming and outgoing endpoints are presented from the current entity's view,
while the ontology stores triples in graph source-to-target direction. Correct
validation prevents valid business relationships from being rejected before
admission and protects the frozen ontology's exact triple contract.
"""

from kbfabric_ingest.ingest.lane2_node_reconciliation import (
    ExistingNode,
    MockSemanticSimilarityProvider,
)
from kbfabric_ingest.ingest.relationship_endpoint_resolution import (
    resolve_context_relationship,
)


class _EmptyCandidateReader:
    def fetch_candidates(self, *args, **kwargs):
        return []


def test_incoming_relationship_validates_the_ontology_source_to_target_order():
    """Objective: accept a valid incoming ENFORCED_BY triple for endpoint lookup.

    The test matters because endpoint resolution runs from both entities' views
    of a relationship. Invariant: incoming Rule -> Regulation evidence passes
    ontology validation and proceeds to candidate resolution rather than being
    rejected as Regulation -> Rule.
    """
    result = resolve_context_relationship(
        source_type="Regulation",
        source_name="Consumer Credit Disclosure Standard",
        source_entity_id="reg-001",
        direction="incoming",
        rel_type="ENFORCED_BY",
        target_type="Rule",
        target_value="RULE-001",
        domain="consumer_retail",
        ontology={
            "relationship_types": [
                {
                    "name": "ENFORCED_BY",
                    "from": ["Rule"],
                    "to": ["Regulation"],
                }
            ]
        },
        candidate_reader=_EmptyCandidateReader(),
    )

    assert result.decision == "RELATIONSHIP_REVIEW_REQUIRED"
    assert result.reason_code == "no_eligible_candidates_below_auto_resolution_threshold"


def test_outgoing_relationship_rejects_the_reversed_ontology_triple():
    """Objective: reject an outgoing edge whose endpoint order violates ontology.

    This matters because extraction suggestions must not bypass exact graph
    contracts. Invariant: Regulation -> Rule is rejected when only Rule ->
    Regulation is permitted.
    """
    result = resolve_context_relationship(
        source_type="Regulation",
        source_name="Consumer Credit Disclosure Standard",
        source_entity_id="reg-001",
        direction="outgoing",
        rel_type="ENFORCED_BY",
        target_type="Rule",
        target_value="RULE-001",
        domain="consumer_retail",
        ontology={
            "relationship_types": [
                {
                    "name": "ENFORCED_BY",
                    "from": ["Rule"],
                    "to": ["Regulation"],
                }
            ]
        },
        candidate_reader=_EmptyCandidateReader(),
    )

    assert result.decision == "REJECTED"
    assert result.reason_code == "invalid_relationship_triple"


def test_batch_local_endpoint_without_cosine_evidence_goes_to_hitl():
    """Objective: require the same lexical/cosine confidence for batch candidates.

    Invariant: an exact lexical reference with no vector evidence has a score
    below 0.8 and is reviewable; it does not bypass the shared statistical gate.
    """
    result = resolve_context_relationship(
        source_type="Process",
        source_name="Credit Limit Review",
        source_entity_id="process-temp",
        direction="outgoing",
        rel_type="GOVERNED_BY",
        target_type="Rule",
        target_value="RULE-SYN-001",
        domain="consumer_retail",
        ontology={
            "relationship_types": [
                {"name": "GOVERNED_BY", "from": ["Process"], "to": ["Rule"]}
            ]
        },
        candidate_reader=_EmptyCandidateReader(),
        batch_candidates=[
            ExistingNode(
                entity_id="batch:rule-syn-001",
                name="RULE-SYN-001",
                name_display="RULE-SYN-001",
                kg_node_type="Rule",
                labels=["Rule"],
            ),
        ],
        semantic=_FixedSemantic(),
    )

    assert result.decision == "RELATIONSHIP_REVIEW_REQUIRED"
    assert result.selected_entity_id is None
    assert result.confidence < 0.8
    assert result.reason_code == "statistical_match_at_or_below_auto_resolution_threshold"


class _AmbiguousCandidateReader:
    def fetch_candidates(self, *args, **kwargs):
        return [
            ExistingNode(
                entity_id="rule-1", name="Primary Rule", aliases=["RULE-001"],
                kg_node_type="Rule", labels=["Rule"], embedding=[1.0, 0.0],
            ),
            ExistingNode(
                entity_id="rule-2", name="Alternate Rule", aliases=["RULE-001"],
                kg_node_type="Rule", labels=["Rule"], embedding=[1.0, 0.0],
            ),
        ]


class _FixedSemantic:
    name = "fixed-test"
    is_mock = False

    def embed(self, _text):
        return [1.0, 0.0]


def test_low_score_relationship_endpoint_stays_in_hitl_without_llm_override():
    """Objective: verify low confidence is a deterministic HITL decision.

    Invariant: a legacy LLM callback cannot override the shared 0.8 score gate.
    """
    def forbidden_llm(*_args, **_kwargs):
        raise AssertionError("relationship matching must not call an LLM")

    result = resolve_context_relationship(
        source_type="Process", source_name="Payment Review", source_entity_id="process-1",
        direction="outgoing", rel_type="GOVERNED_BY", target_type="Rule",
        target_value="RULE-001", domain="consumer_retail",
        ontology={"relationship_types": [
            {"name": "GOVERNED_BY", "from": ["Process"], "to": ["Rule"]}
        ]},
        candidate_reader=_AmbiguousCandidateReader(), semantic=MockSemanticSimilarityProvider(),
        llm_callable=forbidden_llm, use_llm_adjudication=True,
        source_context="A payment is reviewed under RULE-001.",
    )
    assert result.decision == "RELATIONSHIP_REVIEW_REQUIRED"
    assert result.selected_entity_id is None
    assert result.confidence <= 0.8

"""Objective: protect candidate ranking evidence contracts used by mapping and relationship resolution.

These checks matter because retrieval errors create duplicate KG entities or attach relationships to
an unrelated endpoint. They protect the invariant that RRF combines independent rankings, while the
cross-encoder sees source and graph evidence and only reorders candidates; it does not turn raw logits
into admission confidence.
"""

from custom_ml.reranking.candidate_ranking import (
    candidate_document,
    cross_encoder_scores,
    reciprocal_rank_fusion,
)
from kbfabric_ingest.ingest.lane2_node_reconciliation import ExistingNode
from kbfabric_ingest.ingest.relationship_endpoint_resolution import rank_context_resolution_candidates


class _Semantic:
    name = "test-semantic"
    is_mock = False

    def embed(self, _text):
        return [1.0, 0.0]


class _Reranker:
    def __init__(self):
        self.pairs = []

    def predict(self, pairs):
        self.pairs = list(pairs)
        return [0.1 if "Wrong" in doc else 4.2 for _, doc in pairs]


def test_rrf_combines_rankings_without_using_raw_score_scales():
    """Objective: ensure lexical and semantic rank lists can be fused consistently."""
    scores = reciprocal_rank_fusion([["a", "b"], ["b", "a"]])
    assert scores["a"] == scores["b"]


def test_candidate_document_contains_description_alias_and_neighborhood():
    """Objective: preserve richer identity evidence for accurate KG candidate reranking."""
    candidate = ExistingNode(
        entity_id="c1", name="Control A", kg_node_type="Control",
        description="Blocks unauthorized payment release", aliases=["Release Gate"],
    )
    document = candidate_document(candidate, [{
        "direction": "outgoing", "rel_type": "MITIGATES", "other_type": "Rule",
        "other_value": "Dual Approval Rule",
    }])
    assert "Blocks unauthorized payment release" in document
    assert "Release Gate" in document
    assert "MITIGATES Rule Dual Approval Rule" in document


def test_cross_encoder_sees_block_query_and_returns_raw_logits():
    """Objective: prove context reaches the model and logits remain unnormalized evidence."""
    reranker = _Reranker()
    candidates = [
        ExistingNode(entity_id="wrong", name="Wrong Control", kg_node_type="Control"),
        ExistingNode(entity_id="right", name="Right Control", kg_node_type="Control"),
    ]
    scores = cross_encoder_scores(
        query="Source block: dual approval blocks payment release",
        candidates=candidates,
        reranker=reranker,
    )
    assert "dual approval" in reranker.pairs[0][0]
    assert scores == {"wrong": 0.1, "right": 4.2}


def test_relationship_candidates_are_reranked_with_graph_neighborhood():
    """Objective: use endpoint descriptions and graph context to improve relationship candidate order."""
    reranker = _Reranker()
    candidates = [
        ExistingNode(entity_id="wrong", name="Wrong Rule", kg_node_type="Rule", description="Wrong"),
        ExistingNode(entity_id="right", name="Payment Release Rule", kg_node_type="Rule", description="Approves payment release"),
    ]
    ranked = rank_context_resolution_candidates(
        target_value="Payment Release Rule", candidates=candidates, semantic=_Semantic(),
        query_text="Control MITIGATES Rule Payment Release Rule; source says dual approval",
        top_k=2, min_fusion=0.0, reranker=reranker,
        graph_neighborhoods={"right": [{
            "direction": "incoming", "rel_type": "MITIGATES", "other_type": "Control",
            "other_value": "Payment Release Control",
        }]},
    )
    assert ranked[0].entity_id == "right"
    assert ranked[0].cross_encoder_score == 4.2
    assert any("MITIGATES Control Payment Release Control" in doc for _, doc in reranker.pairs)


def test_weaviate_document_context_query_is_customer_tenant_domain_and_product_line_scoped(monkeypatch):
    """Objective: protect retrieval isolation for supplementary document evidence.

    This matters because candidate adjudication must not consume another tenant's
    chunks. Invariant: Weaviate context reads constrain customer, tenant, domain,
    document path, and selected product line while returning masked text only.
    """
    from kbfabric_ingest.ingest import context_evidence

    class _Query:
        def __init__(self):
            self.where = None

        def get(self, _class_name, properties):
            assert properties == ["masked_text", "block_id"]
            return self

        def with_where(self, where):
            self.where = where
            return self

        def with_limit(self, _limit):
            return self

        def do(self):
            return {"data": {"Get": {"Consumer_retail": [
                {"block_id": "old-block", "masked_text": "Prior customer-approved context"},
                {"block_id": "current-block", "masked_text": "Current chunk duplicate"},
            ]}}}

    class _Client:
        def __init__(self):
            self.query = _Query()

    monkeypatch.setattr(context_evidence, "get_store_name_for_domain", lambda _domain: "Consumer_retail")
    reader = context_evidence.WeaviateContextReader({"url": "http://unused"})
    client = _Client()
    reader._client = client
    evidence = reader.fetch_document_context(
        domain="consumer_retail", customer_id="cust-1", tenant_id="tenant-1",
        source_file="spec.docx", current_block_id="current-block",
        product_line_id="pl-1",
    )
    operands = client.query.where["operands"]
    assert {tuple(item["path"]): item["valueString"] for item in operands} == {
        ("customer_id",): "cust-1", ("tenant_id",): "tenant-1",
        ("domain",): "consumer_retail", ("source_file",): "spec.docx",
        ("ingestion_product_line_id",): "pl-1",
    }
    assert evidence == ["Prior customer-approved context"]

from custom_ml.reranking.decision_benchmark import (
    Candidate,
    eligible_candidates,
    ranking_metrics,
    reciprocal_rank_fusion,
)


def test_rrf_combines_lexical_and_semantic_ranks_deterministically():
    """Objective: verify two independent retrieval rankings combine through RRF. This matters because the benchmark must compare decision models on a stable shared candidate shortlist; invariant: a candidate ranked well by both channels outranks one supported by only one channel, with deterministic ties."""
    candidates = [
        Candidate("lexical", "lexical match", 1, None),
        Candidate("both", "both channels", 2, 1),
        Candidate("semantic", "semantic match", None, 2),
    ]

    assert [item.candidate_id for item in reciprocal_rank_fusion(candidates)] == [
        "both",
        "lexical",
        "semantic",
    ]


def test_metrics_penalize_wrong_link_and_no_match_false_link():
    """Objective: verify ranking evaluation detects missed truth and unsafe links. This matters because entity reuse and relationship endpoints must not silently attach to a wrong KG node; invariant: only the labeled target earns reciprocal rank, and any selected candidate for a no-match case is a false link."""
    assert ranking_metrics(["wrong", "right"], "right") == {
        "top1_correct": False,
        "reciprocal_rank": 0.5,
        "false_link": False,
    }
    assert ranking_metrics(["wrong"], None)["false_link"] is True



def test_hard_type_gate_removes_ineligible_candidates_before_ranking():
    """Objective: verify endpoint-type eligibility is a hard pre-ranking gate. This matters because semantically similar nodes of the wrong canonical type must never become mapping or relationship candidates; invariant: ranking only receives candidates matching the required type."""
    candidates = [
        Candidate("rule", "Rule candidate", 1, 1, "Rule"),
        Candidate("control", "Control candidate", 2, 2, "Control"),
    ]

    assert [c.candidate_id for c in eligible_candidates(candidates, required_node_type="Control")] == ["control"]


def test_synthetic_ground_truth_cases_are_auditable_and_type_valid():
    """Objective: verify every benchmark label has source evidence and satisfies its target-type gate. This matters because a misleading or internally inconsistent gold set would favor the wrong resolver; invariant: each case has evidence, and any positive label is present among eligible candidates with the required type."""
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures/kg_candidate_cases.json"
    cases = json.loads(fixture.read_text())
    assert len(cases) >= 10
    for case in cases:
        assert case["evidence"].strip()
        candidates = [Candidate(**item) for item in case["candidates"]]
        eligible = eligible_candidates(
            candidates, required_node_type=case["required_node_type"]
        )
        if case["gold_candidate_id"] is not None:
            assert any(c.candidate_id == case["gold_candidate_id"] for c in eligible), case

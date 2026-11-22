"""Unit tests for Lane 2 two-pass IE prototype (mocked LLM; no Neo4j/GraphDB)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from kbfabric_ingest.ingest.config import get_ontology
from kbfabric_ingest.ingest.lane2_two_pass import (
    PASS1_PROMPT_ID,
    CategoryExtractionPrompt,
    ClassificationResult,
    MockCategoryExtractionProvider,
    YamlOntologyProvider,
    classify_block,
    extract_for_category,
    lane2_result_to_ie_dict,
    process_lane2_block,
)
from kbfabric_ingest.ingest.models import CanonicalBlock
from kbfabric_ingest.ingest.ontology_prompt import get_allowed_category_names


def _make_block(
    *,
    block_id: str = "blk-1",
    text: str = "Sample text.",
    source_file: str = "docs/sample.docx",
    domain: str = "corporate_commercial",
    heading: str | None = "Loan Servicing",
    block_index: int = 0,
    block_type: str = "paragraph",
) -> CanonicalBlock:
    metadata: dict[str, Any] = {}
    if heading is not None:
        metadata["heading_context_raw"] = heading
    return CanonicalBlock(
        block_id=block_id,
        source_file=source_file,
        file_type="docx",
        domain=domain,
        page_number=1,
        block_index=block_index,
        block_type=block_type,
        text=text,
        image_bytes=None,
        metadata=metadata,
        semantic_unit_id="su-1",
        source_block_ids=["src-a", "src-b"],
    )


def _process_text() -> str:
    # ~1500 chars representative Process block
    body = (
        "Loan disbursement initiation: The maker enters the disbursement instruction in LOS_Core "
        "specifying account number, amount, and value date. The system validates available limit "
        "against sanctioned amount. On success the maker submits for checker review. The checker "
        "approves in LOS_Core. On dual approval the disbursement request is transmitted to Core "
        "Banking for fund transfer execution. Related actors include Relationship Manager oversight "
        "when exceptions are raised. Steps must be completed in sequence before funds leave the bank."
    )
    pad = " Additional operational detail for maker-checker control and audit trail. "
    text = body
    while len(text) < 1500:
        text += pad
    return text[:1500]


def _rule_text() -> str:
    body = (
        "Interest accrual ceases when account reaches 90+ DPD and is classified as Non-Accrual of "
        "Business (NAB). If the account cures below 90 DPD then accrual may resume after compliance "
        "review. Repayment priority follows: Interest → Charges → Tax → Principal. When a pricing "
        "override is requested, the system must log user ID, timestamp, old rate, new rate, and reason."
    )
    pad = " Conditional outcome language is required for rule classification and extraction. "
    text = body
    while len(text) < 1500:
        text += pad
    return text[:1500]


def _requirement_text() -> str:
    body = (
        "FR-04: The system shall auto-generate a repayment schedule (EMI or bullet) upon loan booking, "
        "reflecting selected interest type, tenor, and repayment frequency. The system must validate "
        "that collateral value exceeds 120% of loan amount before approval can proceed."
    )
    pad = " Functional requirement language uses shall/must obligations without conditional outcomes. "
    text = body
    while len(text) < 1500:
        text += pad
    return text[:1500]


class ScriptedLLM:
    """Deterministic LLM stub: returns scripted responses in call order."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system_prompt: str, user_content: str) -> tuple[str, dict[str, Any]]:
        self.calls.append((system_prompt, user_content))
        if not self.responses:
            raise AssertionError("ScriptedLLM has no remaining responses")
        return self.responses.pop(0), {"duration_ms": 12.5, "token_usage": {"prompt": 10, "completion": 5}}


def test_classification_parse_and_validation():
    ontology = get_ontology()
    allowed = get_allowed_category_names(ontology)
    assert "Process" in allowed

    llm = ScriptedLLM(
        [
            json.dumps(
                {
                    "category": "Process",
                    "confidence": 0.91,
                    "reasoning_summary": "Named procedure with maker-checker steps.",
                }
            )
        ]
    )
    result = classify_block(_make_block(text=_process_text()), llm_callable=llm, ontology=ontology)
    assert result.is_valid
    assert result.category == "Process"
    assert result.confidence == pytest.approx(0.91)
    assert not result.validation_errors
    assert result.prompt_id == PASS1_PROMPT_ID


def test_pass2_prompt_selection_by_category():
    ontology = get_ontology()
    provider = YamlOntologyProvider(ontology)
    process_pack = provider.get_prompt("Process")
    rule_pack = provider.get_prompt("Rule")
    assert process_pack.category == "Process"
    assert rule_pack.category == "Rule"
    assert process_pack.prompt_id != rule_pack.prompt_id
    assert "Process" in process_pack.system_prompt
    assert "Rule" in rule_pack.system_prompt
    # Full multi-category ontology dump should not be resent
    assert "BLOCK CATEGORIES (classify each block" not in process_pack.system_prompt


def test_process_classification_invokes_only_process_extraction():
    ontology = get_ontology()
    provider = MagicMock(spec=MockCategoryExtractionProvider)
    provider.is_mocked = True
    process_pack = YamlOntologyProvider(ontology).get_prompt("Process")
    provider.get_prompt.return_value = process_pack

    llm = ScriptedLLM(
        [
            json.dumps(
                {"category": "Process", "confidence": 0.9, "reasoning_summary": "Executable steps."}
            ),
            json.dumps(
                {
                    "entities": [
                        {"type": "Process", "value": "Loan Disbursement Initiation", "confidence": 0.95},
                        {"type": "System", "value": "LOS_Core", "confidence": 0.9},
                    ],
                    "relationships": [
                        {
                            "from_type": "Process",
                            "from_value": "Loan Disbursement Initiation",
                            "rel_type": "USES",
                            "to_type": "System",
                            "to_value": "LOS_Core",
                            "confidence": 0.9,
                        }
                    ],
                    "confidence": 0.88,
                    "provenance": None,
                }
            ),
        ]
    )
    result = process_lane2_block(
        _make_block(text=_process_text(), block_id="proc-1"),
        llm_callable=llm,
        prompt_provider=provider,
        ontology=ontology,
    )
    assert result.pass2_invoked
    assert result.classification.category == "Process"
    provider.get_prompt.assert_called_once_with("Process")
    assert "Process" in llm.calls[1][0]
    assert "BLOCK CATEGORIES (classify each block" not in llm.calls[1][0]


def test_rule_classification_invokes_only_rule_extraction():
    ontology = get_ontology()
    provider = MagicMock(spec=MockCategoryExtractionProvider)
    provider.is_mocked = True
    rule_pack = YamlOntologyProvider(ontology).get_prompt("Rule")
    provider.get_prompt.return_value = rule_pack

    llm = ScriptedLLM(
        [
            json.dumps(
                {"category": "Rule", "confidence": 0.93, "reasoning_summary": "If/then with consequence."}
            ),
            json.dumps(
                {
                    "entities": [
                        {"type": "Rule", "value": "NAB Classification at 90 DPD", "confidence": 0.95}
                    ],
                    "relationships": [
                        {
                            "from_type": "Process",
                            "from_value": "Interest Accrual",
                            "rel_type": "GOVERNED_BY",
                            "to_type": "Rule",
                            "to_value": "NAB Classification at 90 DPD",
                            "confidence": 0.9,
                        }
                    ],
                    "confidence": 0.9,
                    "provenance": None,
                }
            ),
        ]
    )
    result = process_lane2_block(
        _make_block(text=_rule_text(), block_id="rule-1"),
        llm_callable=llm,
        prompt_provider=provider,
        ontology=ontology,
    )
    assert result.classification.category == "Rule"
    provider.get_prompt.assert_called_once_with("Rule")
    assert result.extraction is not None
    assert any(e["type"] == "Rule" for e in result.extraction.entities)


def test_invalid_classification_does_not_invoke_pass2():
    ontology = get_ontology()
    provider = MagicMock(spec=MockCategoryExtractionProvider)
    provider.is_mocked = False

    llm = ScriptedLLM(
        [json.dumps({"category": "Actor", "confidence": 0.8, "reasoning_summary": "Wrong taxonomy."})]
    )
    result = process_lane2_block(
        _make_block(text=_process_text()),
        llm_callable=llm,
        prompt_provider=provider,
        ontology=ontology,
    )
    assert not result.pass2_invoked
    assert result.extraction is None
    assert not result.success
    provider.get_prompt.assert_not_called()
    assert len(llm.calls) == 1


def test_malformed_llm_response_handled_without_crash():
    """Verify malformed Pass 1/Pass 2 responses fail safely and preserve namespaced aggregate errors."""
    ontology = get_ontology()
    llm = ScriptedLLM(["this is not json {{{"])
    result = classify_block(_make_block(), llm_callable=llm, ontology=ontology)
    assert not result.is_valid
    assert "malformed_json" in result.validation_errors

    # Pass 2 malformed after valid classification
    provider = YamlOntologyProvider(ontology)
    llm2 = ScriptedLLM(
        [
            json.dumps({"category": "Process", "confidence": 0.9, "reasoning_summary": "ok"}),
            "NOT JSON AT ALL",
        ]
    )
    combined = process_lane2_block(
        _make_block(text=_process_text()),
        llm_callable=llm2,
        prompt_provider=provider,
        ontology=ontology,
    )
    assert combined.pass2_invoked
    assert combined.extraction is not None
    assert "Process:malformed_json" in combined.extraction.validation_errors
    assert not combined.success


def test_mock_provider_is_injectable_and_replaceable():
    """Verify extraction providers remain replaceable while aggregate results preserve provider lineage."""
    ontology = get_ontology()
    mock = MockCategoryExtractionProvider(ontology)
    assert mock.is_mocked is True
    pack = mock.get_prompt("Process")
    assert pack.is_mocked is True
    assert "MOCK" in pack.system_prompt
    assert pack.prompt_id.endswith("_mock")

    class ReplacementProvider:
        is_mocked = False

        def get_prompt(self, category: str) -> CategoryExtractionPrompt:
            return CategoryExtractionPrompt(
                category=category,
                prompt_id=f"custom_{category}",
                prompt_version="v9",
                system_prompt=f"CUSTOM EXTRACT {category}",
                allowed_entity_types=["Process", "System"],
                allowed_relationship_types=["USES"],
                is_mocked=False,
            )

        def supported_categories(self) -> list[str]:
            return ["Process"]

    llm = ScriptedLLM(
        [
            json.dumps({"category": "Process", "confidence": 0.9, "reasoning_summary": "ok"}),
            json.dumps(
                {
                    "entities": [{"type": "Process", "value": "X", "confidence": 0.8}],
                    "relationships": [],
                    "confidence": 0.8,
                }
            ),
        ]
    )
    result = process_lane2_block(
        _make_block(text=_process_text()),
        llm_callable=llm,
        prompt_provider=ReplacementProvider(),
        ontology=ontology,
    )
    assert result.extraction is not None
    assert result.extraction.prompt_id == "merged:custom_Process"
    assert "CUSTOM EXTRACT Process" in llm.calls[1][0]


def test_logs_include_classification_and_extraction_summary_fields():
    ontology = get_ontology()
    captured: list[dict[str, Any]] = []

    # Capture structlog-style kwargs by wrapping process path logger
    import kbfabric_ingest.ingest.lane2_two_pass as mod

    original_info = mod.logger.info

    def _capturing_info(event, *args, **kwargs):
        captured.append({"event": event, **kwargs})
        return original_info(event, *args, **kwargs)

    mod.logger.info = _capturing_info  # type: ignore[method-assign]
    try:
        result = process_lane2_block(
            _make_block(text=_process_text(), block_id="log-1"),
            llm_callable=ScriptedLLM(
                [
                    json.dumps(
                        {
                            "category": "Process",
                            "confidence": 0.9,
                            "reasoning_summary": "Steps present.",
                        }
                    ),
                    json.dumps(
                        {
                            "entities": [
                                {
                                    "type": "Process",
                                    "value": "Loan Disbursement",
                                    "confidence": 0.9,
                                }
                            ],
                            "relationships": [],
                            "confidence": 0.85,
                        }
                    ),
                ]
            ),
            prompt_provider=MockCategoryExtractionProvider(ontology),
            ontology=ontology,
        )
    finally:
        mod.logger.info = original_info  # type: ignore[method-assign]

    events = {c["event"] for c in captured}
    assert "lane2.two_pass.start" in events
    assert "lane2.two_pass.pass1.start" in events
    assert "lane2.two_pass.pass1.end" in events
    assert "lane2.two_pass.pass1" in events
    assert "lane2.two_pass.pass1.gate" in events
    assert "lane2.two_pass.pass2.start" in events
    assert "lane2.two_pass.pass2.prompt_selected" in events
    assert "lane2.two_pass.pass2.end" in events
    assert "lane2.two_pass.pass2" in events
    assert "lane2.two_pass.complete" in events
    complete = next(c for c in captured if c["event"] == "lane2.two_pass.complete")
    gate = next(c for c in captured if c["event"] == "lane2.two_pass.pass1.gate")
    assert gate["will_invoke_pass2"] is True
    assert complete["pass1_category"] == "Process"
    assert complete["entity_count"] >= 1
    assert "extracted_entity_types" in complete
    assert "total_duration_ms" in complete
    assert complete["mocked_ontology_or_prompt_provider"] is True
    assert result.classification.category == "Process"


def test_processing_runs_without_neo4j_graphdb():
    """Ensure prototype path does not import/call graph writers."""
    import kbfabric_ingest.ingest.lane2_two_pass as mod

    assert not hasattr(mod, "KGWriter")
    assert "neo4j" not in mod.__dict__
    ontology = get_ontology()
    llm = ScriptedLLM(
        [
            json.dumps({"category": "Rule", "confidence": 0.9, "reasoning_summary": "Conditional."}),
            json.dumps(
                {
                    "entities": [{"type": "Rule", "value": "Repayment Priority Order", "confidence": 0.9}],
                    "relationships": [],
                    "confidence": 0.8,
                }
            ),
        ]
    )
    result = process_lane2_block(
        _make_block(text=_rule_text()),
        llm_callable=llm,
        prompt_provider=YamlOntologyProvider(ontology),
        ontology=ontology,
    )
    assert result.pass2_invoked
    # No graph client on result
    assert not hasattr(result, "neo4j")
    assert result.model_dump().get("graph_writes") is None


def test_sample_blocks_produce_reviewable_outputs():
    ontology = get_ontology()
    samples = [
        (
            _make_block(block_id="sample-process", text=_process_text(), heading="Disbursement"),
            "Process",
            [
                {"type": "Process", "value": "Loan Disbursement Initiation", "confidence": 0.95},
                {"type": "Actor", "value": "Maker", "confidence": 0.85},
            ],
        ),
        (
            _make_block(block_id="sample-rule", text=_rule_text(), heading="Interest Accrual"),
            "Rule",
            [{"type": "Rule", "value": "NAB Classification at 90 DPD", "confidence": 0.95}],
        ),
        (
            _make_block(block_id="sample-req", text=_requirement_text(), heading="Requirements"),
            "Requirement",
            [{"type": "Requirement", "value": "FR-04", "confidence": 0.95}],
        ),
    ]

    outputs = []
    for block, category, entities in samples:
        # Build extraction allowing entity types from YAML pack
        pack = YamlOntologyProvider(ontology).get_prompt(category)
        # Filter entities to those allowed by pack (Actor may need inclusion)
        allowed = set(pack.allowed_entity_types)
        filtered = [e for e in entities if e["type"] in allowed]
        if not filtered:
            filtered = [{"type": pack.allowed_entity_types[0], "value": "Sample", "confidence": 0.8}]

        llm = ScriptedLLM(
            [
                json.dumps(
                    {
                        "category": category,
                        "confidence": 0.9,
                        "reasoning_summary": f"Classified as {category}.",
                    }
                ),
                json.dumps(
                    {
                        "entities": filtered,
                        "relationships": [],
                        "confidence": 0.87,
                        "provenance": block.source_file,
                    }
                ),
            ]
        )
        result = process_lane2_block(
            block,
            llm_callable=llm,
            prompt_provider=MockCategoryExtractionProvider(ontology),
            ontology=ontology,
        )
        assert result.success
        assert result.classification.category == category
        assert result.extraction is not None
        assert result.character_count == len(block.text)
        assert result.heading_context is not None
        outputs.append(result.model_dump())

    assert len(outputs) == 3
    assert all("classification" in o and "extraction" in o for o in outputs)


def test_extract_for_category_skips_invalid_category():
    ontology = get_ontology()
    llm = ScriptedLLM([])
    result = extract_for_category(
        _make_block(),
        "NotACategory",
        llm_callable=llm,
        prompt_provider=YamlOntologyProvider(ontology),
        ontology=ontology,
        classification=ClassificationResult(category=None, validation_errors=["invalid"]),
    )
    assert result.validation_errors
    assert llm.calls == []


def _runtime_lane2_ontology() -> dict:
    """Provide the persisted-ontology shape required by Lane 2 projection tests."""
    return {
        "block_categories": [
            {"name": "Requirement", "maps_to_node": "Requirement"},
            {"name": "Narrative", "maps_to_node": "_SKIP_KG"},
        ]
    }


def test_lane2_result_to_ie_dict_success_and_fallback():
    """Verify Lane 2 mapping comes from the frozen runtime ontology, preserving governed node admission."""
    ontology = get_ontology()
    allowed = get_allowed_category_names(ontology)
    assert "Process" in allowed

    llm = ScriptedLLM(
        [
            json.dumps(
                {
                    "category": "Process",
                    "confidence": 0.91,
                    "reasoning_summary": "operational steps",
                }
            ),
            json.dumps(
                {
                    "entities": [
                        {"type": "Process", "value": "Disbursement", "confidence": 0.9},
                        {"type": "System", "value": "LOS_Core", "confidence": 0.9},
                    ],
                    "relationships": [
                        {
                            "from_type": "Process",
                            "from_value": "Disbursement",
                            "rel_type": "USES",
                            "to_type": "System",
                            "to_value": "LOS_Core",
                            "confidence": 0.9,
                        }
                    ],
                    "confidence": 0.88,
                    "provenance": None,
                }
            ),
        ]
    )
    result = process_lane2_block(
        _make_block(text=_process_text()),
        llm_callable=llm,
        prompt_provider=YamlOntologyProvider(ontology),
        ontology=ontology,
    )
    assert result.success
    runtime_ontology = {
        "block_categories": [
            {"name": "Process", "maps_to_node": "CustomerProcess"},
            {"name": "Narrative", "maps_to_node": "_SKIP_KG"},
        ]
    }
    ie = lane2_result_to_ie_dict(result, runtime_ontology)
    assert ie["block_category"] == "Process"
    assert ie["kg_node_type"] == "CustomerProcess"
    assert any(e.get("value") == "Disbursement" for e in ie["entities"])
    assert isinstance(ie["relationships"], list)
    assert ie["spine_node"] == "domain_root"

    # Invalid classification → Narrative empty extraction
    bad = process_lane2_block(
        _make_block(text="x"),
        llm_callable=ScriptedLLM(
            [json.dumps({"category": "NotReal", "confidence": 0.1, "reasoning_summary": "n/a"})]
        ),
        prompt_provider=YamlOntologyProvider(ontology),
        ontology=ontology,
    )
    ie_bad = lane2_result_to_ie_dict(bad, _runtime_lane2_ontology())
    assert ie_bad["block_category"] == "Narrative"
    assert ie_bad["entities"] == []
    assert ie_bad["relationships"] == []


def test_pass2_preserves_entity_description_and_aliases():
    ontology = get_ontology()
    provider = MagicMock(spec=MockCategoryExtractionProvider)
    provider.is_mocked = True
    req_pack = YamlOntologyProvider(ontology).get_prompt("Requirement")
    provider.get_prompt.return_value = req_pack

    statement = (
        "The system shall auto-generate a repayment schedule upon loan booking."
    )
    llm = ScriptedLLM(
        [
            json.dumps(
                {
                    "entities": [
                        {
                            "type": "Requirement",
                            "value": "FR-04",
                            "description": statement,
                            "aliases": ["Functional Req 04"],
                            "confidence": 0.95,
                        }
                    ],
                    "relationships": [],
                    "confidence": 0.9,
                    "provenance": None,
                }
            ),
        ]
    )
    result = extract_for_category(
        _make_block(text=f"FR-04: {statement}"),
        "Requirement",
        llm_callable=llm,
        prompt_provider=provider,
        ontology=ontology,
    )

    assert len(result.entities) == 1
    entity = result.entities[0]
    assert entity["value"] == "FR-04"
    assert entity["description"] == statement
    assert entity["aliases"] == ["Functional Req 04"]

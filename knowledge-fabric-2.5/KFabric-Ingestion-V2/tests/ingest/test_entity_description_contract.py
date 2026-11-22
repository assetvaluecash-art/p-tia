"""Regression tests for consistent source-grounded entity descriptions."""

from kbfabric_ingest.ingest.lane2_two_pass import (
    CategoryExtractionPrompt,
    YamlOntologyProvider,
    _entity_policy_rejection_reason,
    _normalize_pass2_entity,
)
from kbfabric_ingest.ingest.models import CanonicalBlock
from kbfabric_ingest.ingest.requirement_entity_fields import (
    extract_entity_description_from_text,
)


def _block(text: str) -> CanonicalBlock:
    return CanonicalBlock(
        block_id="block-1", source_file="source.md", file_type="md",
        domain="banking", block_index=0, block_type="paragraph",
        text=text, metadata={},
    )

def _pack() -> CategoryExtractionPrompt:
    return CategoryExtractionPrompt(
        category="Regulation", prompt_id="test", prompt_version="v1",
        system_prompt="", allowed_entity_types=["Regulation"],
        allowed_relationship_types=[],
    )

def test_entity_descriptions_keep_identity_and_explanation_separate_for_types():
    """Objective: verify type-aware entities retain source-grounded descriptions.

    This matters because IDs alone are not useful to reviewers or retrieval.
    Invariant: Regulation, Control, and Rule identities remain in value while
    explicitly labeled source detail is normalized into description.
    """
    cases = [
        ("Regulation", "PCI-DSS: Requires protection of stored cardholder data."),
        ("Control", "Control: Approval gate checks sanctions screening completion."),
        ("Rule", "Rule: If DPD exceeds 90 days, freeze the account."),
    ]
    for entity_type, text in cases:
        value = text.split(":", 1)[0]
        description = extract_entity_description_from_text(text, entity_type, value)
        assert description == text.split(":", 1)[1].strip()
        assert value not in description

def test_pass2_preserves_description_and_does_not_invent_id_only_detail():
    """Objective: verify Pass 2 normalizes descriptions without fabricating them.

    This matters because unsupported descriptions mislead compliance review.
    Invariant: explicit model/source descriptions survive normalization, while
    an ID-only regulation remains valid with no description.
    """
    source = _block("REG-42: Requires retention of transaction records for seven years.")
    described = _normalize_pass2_entity(
        {"value": "REG-42", "confidence": 0.9}, entity_type="Regulation",
        category="Regulation", pack=_pack(), block=source,
    )
    assert described["value"] == "REG-42"
    assert described["description"] == "Requires retention of transaction records for seven years."

    id_only = _normalize_pass2_entity(
        {"value": "REG-99", "confidence": 0.9}, entity_type="Regulation",
        category="Regulation", pack=_pack(), block=_block("REG-99"),
    )
    assert id_only["value"] == "REG-99"
    assert "description" not in id_only

def test_explicit_pass2_description_takes_precedence_over_source_fallback():
    """Objective: verify explicit extracted detail is preserved over fallback.

    This matters because a shared normalizer must not replace model-provided
    source interpretation with a weaker ID-prefix heuristic. Invariant: a
    nonempty extracted description is retained unchanged (apart from trimming).
    """
    entity = _normalize_pass2_entity(
        {"value": "CTRL-7", "description": "  Checks dual approval.  ", "confidence": 0.9},
        entity_type="Control", category="Control", pack=_pack(),
        block=_block("CTRL-7: control identifier"),
    )
    assert entity["description"] == "Checks dual approval."


def test_pass2_renders_runtime_guidance_for_every_allowed_entity_type():
    """Objective: verify Pass 2 includes policy guidance for related node types.

    This matters because a primary Requirement may also extract related Process
    or System nodes. Invariant: each allowed type's runtime definition, examples,
    and anti-examples reach the extraction prompt from the composed ontology.
    """
    ontology = {
        "block_categories": [{"name": "Requirement", "maps_to_node": "Requirement"}],
        "node_types": [
            {"name": "Requirement", "description": "An obligation.", "ie_examples": [{
                "entities": [{"type": "Requirement", "value": "REQ-1"},
                          {"type": "System", "value": "Named Portal"}],
                "relationships": [{"rel_type": "IMPACTS"}],
            }]},
            {"name": "System", "description": "Named deployed software.",
             "name_pattern": "Product name", "examples": ["Named Portal"],
             "anti_examples": ["Do not extract generic nouns."],
             "reject_names": ["Storage"]},
            {"name": "Process", "description": "Executable business procedure.",
             "anti_examples": ["Do not extract abstract stages."]},
        ],
        "relationship_types": [{
            "name": "IMPACTS",
            "allowed_triples": [
                {"from": "Requirement", "to": "System"},
                {"from": "Requirement", "to": "Process"},
            ],
        }],
    }
    pack = YamlOntologyProvider(ontology).get_prompt("Requirement")
    assert "  System:\n" in pack.system_prompt
    assert "Named deployed software." in pack.system_prompt
    assert "Do not extract generic nouns." in pack.system_prompt
    assert "  Process:\n" in pack.system_prompt
    assert "Do not extract abstract stages." in pack.system_prompt
    assert pack.entity_type_guidance["System"]["reject_names"] == ["Storage"]

def test_policy_defined_generic_entity_names_are_rejected_exactly():
    """Objective: verify exact generic-name exclusions are enforced by policy.

    This matters because prompt instructions alone can still yield generic
    nodes. Invariant: policy-listed generic values are rejected while qualified
    named products remain eligible.
    """
    guidance = {"System": {"reject_names": ["Storage", "System"]}}
    assert _entity_policy_rejection_reason("System", "Storage", guidance)
    assert _entity_policy_rejection_reason("System", "storage", guidance)
    assert _entity_policy_rejection_reason("System", "Card Storage Platform", guidance) is None

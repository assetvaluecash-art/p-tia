from pathlib import Path

import yaml

from kbfabric_ingest.ingest.ontology_admission import (
    AdmissionDecision,
    OntologyAdmissionEngine,
)


ONTOLOGY_PATH = Path("config/enterprise_banking_ontology.yaml")


def get_engine() -> OntologyAdmissionEngine:
    with ONTOLOGY_PATH.open() as handle:
        ontology = yaml.safe_load(handle)
    return OntologyAdmissionEngine(ontology)


def relationship(
    from_type: str,
    rel_type: str,
    to_type: str,
) -> dict:
    return {
        "type": rel_type,
        "from_type": from_type,
        "from_value": "source",
        "to_type": to_type,
        "to_value": "target",
        "confidence": 1.0,
        "evidence": "test evidence",
    }


def test_production_ontology_compiles_expected_relationship_contract():
    engine = get_engine()

    assert len(engine.node_types) == 18
    assert len(engine.relationship_types) == 17

    expected = {
        ("SubFunction", "PART_OF", "Function"),
        ("Function", "PART_OF", "ProductLine"),
        ("ProductLine", "PART_OF", "DomainRoot"),
        ("Process", "PART_OF", "Function"),
        ("Requirement", "IMPACTS", "Process"),
        ("Requirement", "IMPACTS", "System"),
        ("Requirement", "IMPACTS", "DataEntity"),
        ("Requirement", "IMPACTS", "Control"),
        ("Process", "USES", "System"),
        ("Process", "PRODUCES", "DataEntity"),
        ("Control", "MITIGATES", "Process"),
        ("Control", "MITIGATES", "Rule"),
    }

    assert expected.issubset(engine.allowed_relationship_triples)


def test_production_ontology_rejects_invalid_exact_triple():
    engine = get_engine()

    result = engine.admit_relationship(
        relationship("Process", "USES", "DataEntity")
    )

    assert result.decision == AdmissionDecision.REJECT
    assert "invalid_relationship_triple" in result.reason_codes


def test_production_ontology_accepts_valid_exact_triple():
    engine = get_engine()

    result = engine.admit_relationship(
        relationship("Process", "USES", "System")
    )

    assert result.decision == AdmissionDecision.ACCEPT

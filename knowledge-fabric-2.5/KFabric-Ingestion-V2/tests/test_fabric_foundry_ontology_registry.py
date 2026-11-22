from pathlib import Path

from kbfabric_ingest.fabric_foundry.ontology.registry import (
    get_ontology_definitions,
    get_ontology_definition,
    get_ontology_path,
)


def test_registered_ontologies():
    ontologies = get_ontology_definitions()

    assert len(ontologies) == 3

    ontology_ids = {ontology.ontology_id for ontology in ontologies}

    assert ontology_ids == {
        "banking",
        "health_insurance_care_services",
        "insurance",
    }


def test_banking_ontology_resolution():
    ontology = get_ontology_definition("banking")

    assert ontology is not None
    assert ontology.ontology_id == "banking"
    assert ontology.ontology_name == "Enterprise Banking"
    assert ontology.version == "5.0.1"
    assert ontology.industry_id == "banking"
    assert ontology.status == "ACTIVE"


def test_healthcare_ontology_resolution():
    ontology = get_ontology_definition("health_insurance_care_services")

    assert ontology is not None
    assert ontology.ontology_id == "health_insurance_care_services"
    assert ontology.ontology_name == "Health Insurance & Care Services"
    assert ontology.version == "1.0.0"
    assert ontology.industry_id == "health_insurance_care_services"
    assert ontology.status == "ACTIVE"


def test_insurance_ontology_resolution():
    ontology = get_ontology_definition("insurance")

    assert ontology is not None
    assert ontology.ontology_id == "insurance"
    assert ontology.ontology_name == "Insurance & Retirement Services"
    assert ontology.version == "1.0.0"
    assert ontology.industry_id == "insurance"
    assert ontology.status == "ACTIVE"


def test_case_insensitive_ontology_id():
    ontology = get_ontology_definition("BANKING")

    assert ontology is not None
    assert ontology.ontology_id == "banking"


def test_unknown_ontology_returns_none():
    assert get_ontology_definition("does_not_exist") is None


def test_unknown_version_returns_none():
    assert get_ontology_definition("banking", version="999.0.0") is None


def test_ontology_paths_exist():
    for ontology in get_ontology_definitions():
        path = get_ontology_path(ontology.ontology_id)

        assert path.exists()
        assert path.is_file()


def test_ontology_paths_are_relative_to_project_root():
    project_root = Path(__file__).resolve().parents[1]

    for ontology in get_ontology_definitions():
        path = get_ontology_path(ontology.ontology_id)

        assert path.is_relative_to(project_root)

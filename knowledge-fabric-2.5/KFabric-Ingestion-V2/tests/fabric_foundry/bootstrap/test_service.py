from unittest.mock import Mock

from kbfabric_ingest.fabric_foundry.bootstrap.service import (
    BOOTSTRAP_INDUSTRIES,
    FabricFoundryBootstrap,
)


def build_bootstrap() -> FabricFoundryBootstrap:
    bootstrap = FabricFoundryBootstrap()

    bootstrap.industry_schema = Mock()
    bootstrap.ontology_schema = Mock()
    bootstrap.industry_ontology_schema = Mock()
    bootstrap.customer_schema = Mock()
    bootstrap.hitl_review_schema = Mock()
    bootstrap.ingestion_run_schema = Mock()

    bootstrap.industry_repository = Mock()
    bootstrap.ontology_repository = Mock()
    bootstrap.association_repository = Mock()

    return bootstrap


def test_bootstrap_ensures_all_control_plane_schema() -> None:
    bootstrap = build_bootstrap()

    bootstrap.run()

    bootstrap.industry_schema.ensure.assert_called_once()
    bootstrap.ontology_schema.ensure.assert_called_once()
    bootstrap.industry_ontology_schema.ensure.assert_called_once()
    bootstrap.hitl_review_schema.ensure.assert_called_once()
    bootstrap.ingestion_run_schema.ensure.assert_called_once()


def test_bootstrap_creates_missing_industries() -> None:
    bootstrap = build_bootstrap()
    bootstrap.industry_repository.get_industry.return_value = None
    bootstrap.ontology_repository.get_ontology.return_value = Mock()
    bootstrap.association_repository.get_association.return_value = Mock()

    bootstrap.run()

    assert bootstrap.industry_repository.create_industry.call_count == 3


def test_bootstrap_creates_missing_ontologies() -> None:
    bootstrap = build_bootstrap()
    bootstrap.industry_repository.get_industry.return_value = Mock()
    bootstrap.ontology_repository.get_ontology.return_value = None
    bootstrap.association_repository.get_association.return_value = Mock()

    bootstrap.run()

    assert bootstrap.ontology_repository.create_ontology.call_count == 3


def test_bootstrap_creates_missing_associations() -> None:
    bootstrap = build_bootstrap()
    bootstrap.industry_repository.get_industry.return_value = Mock()
    bootstrap.ontology_repository.get_ontology.return_value = Mock()
    bootstrap.association_repository.get_association.return_value = None

    bootstrap.run()

    assert bootstrap.association_repository.associate.call_count == 3

    for call in bootstrap.association_repository.associate.call_args_list:
        assert call.kwargs["is_default"] is True


def test_bootstrap_is_idempotent_when_records_exist() -> None:
    bootstrap = build_bootstrap()
    bootstrap.industry_repository.get_industry.return_value = Mock()
    bootstrap.ontology_repository.get_ontology.return_value = Mock()
    bootstrap.association_repository.get_association.return_value = Mock()

    bootstrap.run()

    bootstrap.industry_repository.create_industry.assert_not_called()
    bootstrap.ontology_repository.create_ontology.assert_not_called()
    bootstrap.association_repository.associate.assert_not_called()


def test_bootstrap_definitions_match_expected_control_plane() -> None:
    assert len(BOOTSTRAP_INDUSTRIES) == 3

    assert {
        definition.industry_id
        for definition in BOOTSTRAP_INDUSTRIES
    } == {
        "banking",
        "health_insurance_care_services",
        "insurance",
    }

    for definition in BOOTSTRAP_INDUSTRIES:
        assert definition.ontology_id
        assert definition.ontology_name
        assert definition.ontology_version
        assert definition.artifact_path

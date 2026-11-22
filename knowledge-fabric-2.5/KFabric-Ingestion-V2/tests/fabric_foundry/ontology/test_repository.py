from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest

from kbfabric_ingest.fabric_foundry.ontology.repository import (
    OntologyRecord,
    OntologyRepository,
)


def _repository_with_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetchone_result=None,
    fetchall_result=None,
):
    repository = OntologyRepository()

    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_result
    cursor.fetchall.return_value = fetchall_result or []

    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor

    monkeypatch.setattr(repository, "_connect", lambda: connection)

    return repository, connection, cursor


def test_create_ontology_returns_created_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchone_result=(
            "banking",
            "Enterprise Banking",
            "5.0.1",
            "ACTIVE",
            "config/enterprise_banking_ontology.yaml",
            {"source": "foundry"},
        ),
    )

    ontology = repository.create_ontology(
        ontology_id=" Banking ",
        ontology_name="Enterprise Banking",
        version="5.0.1",
        artifact_path="config/enterprise_banking_ontology.yaml",
        metadata={"source": "foundry"},
    )

    assert ontology == OntologyRecord(
        ontology_id="banking",
        ontology_name="Enterprise Banking",
        version="5.0.1",
        status="ACTIVE",
        artifact_path="config/enterprise_banking_ontology.yaml",
        metadata={"source": "foundry"},
    )

    args = cursor.execute.call_args.args

    assert "INSERT INTO ontology" in args[0]
    assert args[1][0] == "banking"
    assert args[1][1] == "Enterprise Banking"
    assert args[1][2] == "5.0.1"
    assert args[1][3] == "config/enterprise_banking_ontology.yaml"
    assert '"source": "foundry"' in args[1][4]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ontology_id", ""),
        ("ontology_name", ""),
        ("version", ""),
        ("artifact_path", ""),
    ],
)
def test_create_ontology_rejects_empty_required_fields(
    field: str,
    value: str,
) -> None:
    repository = OntologyRepository()

    kwargs = {
        "ontology_id": "banking",
        "ontology_name": "Enterprise Banking",
        "version": "5.0.1",
        "artifact_path": "config/enterprise_banking_ontology.yaml",
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        repository.create_ontology(**kwargs)


def test_create_ontology_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(monkeypatch)

    cursor.execute.side_effect = psycopg.errors.UniqueViolation(
        "duplicate"
    )

    with pytest.raises(ValueError, match="Ontology version already exists"):
        repository.create_ontology(
            ontology_id="banking",
            ontology_name="Enterprise Banking",
            version="5.0.1",
            artifact_path="config/enterprise_banking_ontology.yaml",
        )


def test_get_ontology_by_id_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchone_result=(
            "banking",
            "Enterprise Banking",
            "5.0.1",
            "ACTIVE",
            "config/enterprise_banking_ontology.yaml",
            {},
        ),
    )

    ontology = repository.get_ontology(
        ontology_id=" BANKING ",
        version="5.0.1",
    )

    assert ontology == OntologyRecord(
        ontology_id="banking",
        ontology_name="Enterprise Banking",
        version="5.0.1",
        status="ACTIVE",
        artifact_path="config/enterprise_banking_ontology.yaml",
        metadata={},
    )

    args = cursor.execute.call_args.args
    assert "FROM ontology" in args[0]
    assert "version = %s" in args[0]
    assert args[1] == ("banking", "5.0.1")


def test_get_ontology_by_id_returns_first_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchone_result=(
            "banking",
            "Enterprise Banking",
            "5.0.1",
            "ACTIVE",
            "config/enterprise_banking_ontology.yaml",
            {},
        ),
    )

    ontology = repository.get_ontology(
        ontology_id="banking",
    )

    assert ontology is not None
    assert ontology.version == "5.0.1"

    args = cursor.execute.call_args.args
    assert "ORDER BY version" in args[0]
    assert args[1] == ("banking",)


def test_get_ontology_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, _ = _repository_with_connection(
        monkeypatch,
        fetchone_result=None,
    )

    assert repository.get_ontology(
        ontology_id="does-not-exist",
        version="1.0.0",
    ) is None


def test_list_ontologies_returns_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchall_result=[
            (
                "banking",
                "Enterprise Banking",
                "5.0.1",
                "ACTIVE",
                "config/enterprise_banking_ontology.yaml",
                {},
            ),
            (
                "banking",
                "Enterprise Banking",
                "6.0.0",
                "INACTIVE",
                "config/enterprise_banking_ontology_v6.yaml",
                {},
            ),
        ],
    )

    ontologies = repository.list_ontologies()

    assert ontologies == [
        OntologyRecord(
            ontology_id="banking",
            ontology_name="Enterprise Banking",
            version="5.0.1",
            status="ACTIVE",
            artifact_path="config/enterprise_banking_ontology.yaml",
            metadata={},
        ),
        OntologyRecord(
            ontology_id="banking",
            ontology_name="Enterprise Banking",
            version="6.0.0",
            status="INACTIVE",
            artifact_path="config/enterprise_banking_ontology_v6.yaml",
            metadata={},
        ),
    ]

    args = cursor.execute.call_args.args
    assert "ORDER BY ontology_id, version" in args[0]


def test_list_ontologies_can_filter_by_id_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchall_result=[
            (
                "banking",
                "Enterprise Banking",
                "5.0.1",
                "ACTIVE",
                "config/enterprise_banking_ontology.yaml",
                {},
            ),
        ],
    )

    ontologies = repository.list_ontologies(
        ontology_id=" BANKING ",
        active_only=True,
    )

    assert ontologies == [
        OntologyRecord(
            ontology_id="banking",
            ontology_name="Enterprise Banking",
            version="5.0.1",
            status="ACTIVE",
            artifact_path="config/enterprise_banking_ontology.yaml",
            metadata={},
        ),
    ]

    args = cursor.execute.call_args.args
    assert "ontology_id = %s" in args[0]
    assert "status = 'ACTIVE'" in args[0]
    assert args[1] == ("banking",)

from __future__ import annotations

from unittest.mock import MagicMock

import psycopg
import pytest

from kbfabric_ingest.fabric_foundry.industry.ontology_repository import (
    IndustryOntologyRecord,
    IndustryOntologyRepository,
)


def _repository_with_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetchone_result=None,
    fetchall_result=None,
):
    repository = IndustryOntologyRepository()

    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_result
    cursor.fetchall.return_value = fetchall_result or []

    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor

    monkeypatch.setattr(repository, "_connect", lambda: connection)

    return repository, connection, cursor


def test_associate_returns_created_association(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchone_result=(
            "banking",
            "banking",
            "5.0.1",
            "ACTIVE",
            True,
        ),
    )

    association = repository.associate(
        industry_id=" BANKING ",
        ontology_id=" BANKING ",
        ontology_version="5.0.1",
        is_default=True,
    )

    assert association == IndustryOntologyRecord(
        industry_id="banking",
        ontology_id="banking",
        ontology_version="5.0.1",
        status="ACTIVE",
        is_default=True,
    )

    args = cursor.execute.call_args.args

    assert "INSERT INTO industry_ontology" in args[0]
    assert args[1] == (
        "banking",
        "banking",
        "5.0.1",
        True,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("industry_id", ""),
        ("ontology_id", ""),
        ("ontology_version", ""),
    ],
)
def test_associate_rejects_empty_required_fields(
    field: str,
    value: str,
) -> None:
    repository = IndustryOntologyRepository()

    kwargs = {
        "industry_id": "banking",
        "ontology_id": "banking",
        "ontology_version": "5.0.1",
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        repository.associate(**kwargs)


def test_associate_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(monkeypatch)

    cursor.execute.side_effect = psycopg.errors.UniqueViolation(
        "duplicate"
    )

    with pytest.raises(
        ValueError,
        match="Industry is already associated",
    ):
        repository.associate(
            industry_id="banking",
            ontology_id="banking",
            ontology_version="5.0.1",
        )


def test_list_for_industry_returns_associations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchall_result=[
            (
                "banking",
                "banking",
                "5.0.1",
                "ACTIVE",
                True,
            ),
            (
                "banking",
                "banking",
                "4.0.0",
                "INACTIVE",
                False,
            ),
        ],
    )

    associations = repository.list_for_industry(
        industry_id=" BANKING ",
    )

    assert associations == [
        IndustryOntologyRecord(
            industry_id="banking",
            ontology_id="banking",
            ontology_version="5.0.1",
            status="ACTIVE",
            is_default=True,
        ),
        IndustryOntologyRecord(
            industry_id="banking",
            ontology_id="banking",
            ontology_version="4.0.0",
            status="INACTIVE",
            is_default=False,
        ),
    ]

    args = cursor.execute.call_args.args

    assert "FROM industry_ontology" in args[0]
    assert "ORDER BY is_default DESC" in args[0]
    assert args[1] == ("banking",)


def test_list_for_industry_can_filter_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchall_result=[
            (
                "banking",
                "banking",
                "5.0.1",
                "ACTIVE",
                True,
            ),
        ],
    )

    associations = repository.list_for_industry(
        industry_id="banking",
        active_only=True,
    )

    assert associations == [
        IndustryOntologyRecord(
            industry_id="banking",
            ontology_id="banking",
            ontology_version="5.0.1",
            status="ACTIVE",
            is_default=True,
        ),
    ]

    args = cursor.execute.call_args.args

    assert "status = 'ACTIVE'" in args[0]
    assert args[1] == ("banking",)


def test_get_association_returns_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchone_result=(
            "banking",
            "banking",
            "5.0.1",
            "ACTIVE",
            True,
        ),
    )

    association = repository.get_association(
        industry_id=" BANKING ",
        ontology_id=" BANKING ",
        ontology_version="5.0.1",
    )

    assert association == IndustryOntologyRecord(
        industry_id="banking",
        ontology_id="banking",
        ontology_version="5.0.1",
        status="ACTIVE",
        is_default=True,
    )

    args = cursor.execute.call_args.args

    assert "FROM industry_ontology" in args[0]
    assert args[1] == (
        "banking",
        "banking",
        "5.0.1",
    )


def test_get_association_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, _ = _repository_with_connection(
        monkeypatch,
        fetchone_result=None,
    )

    assert repository.get_association(
        industry_id="banking",
        ontology_id="banking",
        ontology_version="5.0.1",
    ) is None

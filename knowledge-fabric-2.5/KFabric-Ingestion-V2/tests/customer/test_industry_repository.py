from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kbfabric_ingest.fabric_foundry.industry.repository import (
    IndustryRecord,
    IndustryRepository,
)


def _repository_with_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetchone_result=None,
    fetchall_result=None,
):
    repository = IndustryRepository()

    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_result
    cursor.fetchall.return_value = fetchall_result or []

    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor

    monkeypatch.setattr(repository, "_connect", lambda: connection)

    return repository, connection, cursor


def test_create_industry_returns_created_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchone_result=(
            "banking",
            "Banking",
            "Financial services industry",
            "ACTIVE",
            {"source": "foundry"},
        ),
    )

    industry = repository.create_industry(
        industry_id=" Banking ",
        industry_name="Banking",
        description="Financial services industry",
        metadata={"source": "foundry"},
    )

    assert industry == IndustryRecord(
        industry_id="banking",
        industry_name="Banking",
        description="Financial services industry",
        status="ACTIVE",
        metadata={"source": "foundry"},
    )

    args = cursor.execute.call_args.args

    assert "INSERT INTO industry" in args[0]
    assert args[1][0] == "banking"
    assert args[1][1] == "Banking"
    assert args[1][2] == "Financial services industry"
    assert '"source": "foundry"' in args[1][3]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("industry_id", ""),
        ("industry_name", ""),
    ],
)
def test_create_industry_rejects_empty_required_fields(
    field: str,
    value: str,
) -> None:
    repository = IndustryRepository()

    kwargs = {
        "industry_id": "banking",
        "industry_name": "Banking",
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        repository.create_industry(**kwargs)


def test_create_industry_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(monkeypatch)

    cursor.execute.side_effect = __import__(
        "psycopg"
    ).errors.UniqueViolation("duplicate")

    with pytest.raises(ValueError, match="Industry ID already exists"):
        repository.create_industry(
            industry_id="banking",
            industry_name="Banking",
        )


def test_get_industry_returns_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchone_result=(
            "banking",
            "Banking",
            "Financial services industry",
            "ACTIVE",
            {"source": "foundry"},
        ),
    )

    industry = repository.get_industry(
        industry_id=" BANKING ",
    )

    assert industry == IndustryRecord(
        industry_id="banking",
        industry_name="Banking",
        description="Financial services industry",
        status="ACTIVE",
        metadata={"source": "foundry"},
    )

    args = cursor.execute.call_args.args
    assert "FROM industry" in args[0]
    assert args[1] == ("banking",)


def test_get_industry_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, _ = _repository_with_connection(
        monkeypatch,
        fetchone_result=None,
    )

    assert repository.get_industry(
        industry_id="does-not-exist",
    ) is None


def test_list_industries_returns_all_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchall_result=[
            (
                "banking",
                "Banking",
                None,
                "ACTIVE",
                {},
            ),
            (
                "insurance",
                "Insurance & Retirement Services",
                None,
                "INACTIVE",
                {"owner": "foundry"},
            ),
        ],
    )

    industries = repository.list_industries()

    assert industries == [
        IndustryRecord(
            industry_id="banking",
            industry_name="Banking",
            description=None,
            status="ACTIVE",
            metadata={},
        ),
        IndustryRecord(
            industry_id="insurance",
            industry_name="Insurance & Retirement Services",
            description=None,
            status="INACTIVE",
            metadata={"owner": "foundry"},
        ),
    ]

    args = cursor.execute.call_args.args
    assert "FROM industry" in args[0]
    assert "ORDER BY industry_name" in args[0]


def test_list_industries_can_filter_active_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, cursor = _repository_with_connection(
        monkeypatch,
        fetchall_result=[
            (
                "banking",
                "Banking",
                None,
                "ACTIVE",
                {},
            ),
        ],
    )

    industries = repository.list_industries(active_only=True)

    assert industries == [
        IndustryRecord(
            industry_id="banking",
            industry_name="Banking",
            description=None,
            status="ACTIVE",
            metadata={},
        ),
    ]

    args = cursor.execute.call_args.args
    assert "WHERE status = 'ACTIVE'" in args[0]

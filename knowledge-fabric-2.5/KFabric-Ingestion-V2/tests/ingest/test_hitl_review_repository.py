"""Persistence contract tests for HITL reviewer decisions."""

from datetime import datetime, timezone

from kbfabric_ingest.ingest.execution_plan import ExecutionAction, ExecutionPlan
from kbfabric_ingest.ingest.repository.hitl_review_repository import HITLReviewRepository


class _FakeCursor:
    def __init__(self):
        self.calls = []
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        if self.calls and "RETURNING reviewer_action" in self.calls[-1][0]:
            return ("APPROVE_AS_NEW", 2)
        now = datetime.now(timezone.utc)
        return ("APPROVED", now, now)

    def fetchall(self):
        return []


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


def test_decision_and_append_only_event_share_one_scoped_transaction():
    """Objective: verify a reviewer decision and history event are persisted together.

    Auditability matters because every action must remain attributable to a
    reviewer and exact batch/domain/product-line. Invariant: the row update is
    scope constrained and an append-only event is inserted before transaction
    completion; the response records a retryable write state.
    """
    repo = HITLReviewRepository()
    cursor = _FakeCursor()
    repo._connect = lambda: _FakeConnection(cursor)

    result = repo.record_decision(
        review_id="rv-1",
        ingestion_batch_id="batch-1",
        domain_id="consumer_retail",
        product_line_id="pl-retail-cards",
        reviewer_action="APPROVE_AS_NEW",
        reviewer_id="user-1",
        reviewer_notes="Evidence confirms a new process.",
        resolved_plan=ExecutionPlan(
            plan_id="plan-1", action=ExecutionAction.QUEUE_FOR_REVIEW,
            mapping_decision="CREATE_NEW", justification="reviewed",
        ),
    )

    assert result["status"] == "APPROVED"
    assert result["execution_status"] == "PENDING"
    assert len(cursor.calls) == 4
    assert "ontology_context_json->>'domain_id'" in cursor.calls[0][0]
    assert "ontology_context_json->>'product_line_id'" in cursor.calls[0][0]
    assert "INSERT INTO hitl_review_event" in cursor.calls[1][0]
    assert cursor.calls[1][1][4] == "user-1"
    assert "INSERT INTO ingestion_run_event" in cursor.calls[2][0]
    assert cursor.calls[2][1][1] == "hitl.review_decided"
    assert "UPDATE ingestion_run AS r" in cursor.calls[3][0]


def test_write_outcome_and_history_event_are_persisted_together():
    """Objective: verify every KG write attempt has a durable outcome event.

    This makes successes, failures, and retries visible in the review history.
    Invariant: the scoped review status update and append-only write event are
    committed in the same PostgreSQL transaction.
    """
    repo = HITLReviewRepository()
    cursor = _FakeCursor()
    repo._connect = lambda: _FakeConnection(cursor)

    repo.complete_write(
        review_id="rv-1", ingestion_batch_id="batch-1",
        domain_id="consumer_retail", product_line_id="pl-retail-cards",
        result={"nodes": 1, "relationships": 0, "aliases": 0},
    )

    assert len(cursor.calls) == 3
    assert "write_result_json" in cursor.calls[0][0]
    assert "RETURNING reviewer_action, write_attempts" in cursor.calls[0][0]
    assert "WRITE_EXECUTION" in cursor.calls[1][0]
    assert cursor.calls[1][1][1] == "rv-1"
    assert "INSERT INTO ingestion_run_event" in cursor.calls[2][0]
    assert cursor.calls[2][1][1] == "hitl.write_status_changed"


def test_report_audit_query_is_limited_to_batch_domain_and_product_line():
    """A report cannot include reviewer identities from another customer run scope."""
    repo = HITLReviewRepository()
    cursor = _FakeCursor()
    repo._connect = lambda: _FakeConnection(cursor)

    assert repo.list_report_audit(
        ingestion_batch_id="batch-1", domain_id="consumer_retail", product_line_id="pl-cards",
    ) == []

    query, params = cursor.calls[0]
    assert "hitl_review_event" in query
    assert "r.ingestion_batch_id = %(batch_id)s" in query
    assert "ontology_context_json->>'domain_id'" in query
    assert "ontology_context_json->>'product_line_id'" in query
    assert params == {"batch_id": "batch-1", "domain_id": "consumer_retail", "product_line_id": "pl-cards"}

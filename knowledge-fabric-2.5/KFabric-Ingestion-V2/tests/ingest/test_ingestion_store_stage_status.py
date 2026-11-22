"""Plain-English unit checks for the independent knowledge-store stage results."""

from kbfabric_ingest.ingest.ingestion_service import _finalize_store_write_summary


def _stores():
    return {
        "weaviate": {"status": "SUCCEEDED", "chunks_written": 12},
        "neo4j": {"status": "SUCCEEDED", "nodes_written": 4, "relationships_written": 3},
    }


def test_weaviate_success_and_neo4j_failure_are_reported_separately():
    """A partial publish must retain success for one store and failure for the other."""
    summary, stage_status = _finalize_store_write_summary(
        _stores(), [{"step": "neo4j", "error": "connection failed"}], has_blocks=True
    )

    assert stage_status == "FAILED"
    assert summary["weaviate"]["status"] == "SUCCEEDED"
    assert summary["neo4j"]["status"] == "FAILED"
    assert summary["neo4j"]["error_code"] == "DEPENDENCY_FAILURE"


def test_write_validation_failure_marks_only_the_affected_store_failed():
    """A failed store verification must not erase the sibling store's outcome."""
    summary, stage_status = _finalize_store_write_summary(
        _stores(), [{"step": "weaviate_validation", "error": "count mismatch"}], has_blocks=True
    )

    assert stage_status == "FAILED"
    assert summary["weaviate"]["status"] == "FAILED"
    assert summary["weaviate"]["error_code"] == "STORE_WRITE_VALIDATION_FAILED"
    assert summary["neo4j"]["status"] == "SUCCEEDED"


def test_no_enriched_blocks_skip_publish_stage():
    """A run with no enriched content must not claim that stores were written."""
    summary, stage_status = _finalize_store_write_summary(
        _stores(), [], has_blocks=False
    )

    assert stage_status == "SKIPPED"
    assert summary["weaviate"]["status"] == "SKIPPED"
    assert summary["neo4j"]["status"] == "SKIPPED"


def test_upstream_failure_before_publish_leaves_store_stage_pending_for_resume():
    """A failed pre-publish stage must not be mislabeled as a successful skip boundary."""
    summary, stage_status = _finalize_store_write_summary(
        _stores(), [{"step": "enrich", "error": "checkpoint commit failed"}], has_blocks=False
    )

    assert stage_status == "PENDING"
    assert summary["weaviate"]["status"] == "PENDING"
    assert summary["weaviate"]["reason"] == "upstream_stage_failed"
    assert summary["neo4j"]["status"] == "PENDING"
    assert summary["neo4j"]["reason"] == "upstream_stage_failed"

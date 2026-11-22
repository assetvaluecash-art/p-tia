"""English acceptance checks for the business-facing ingestion report."""
from pathlib import Path

from kbfabric_ingest.reports.unified_report import UnifiedIngestionReportGenerator


def test_unified_report_keeps_existing_job_filename_and_leads_with_run_impact(tmp_path):
    """The UI's existing report lookup remains stable while the report foregrounds business outcome."""
    summary = {
        "status": "success",
        "total_files": 1,
        "total_blocks": 24,
        "entities_extracted": 87,
        "relationships_extracted": 42,
        "kg_admission": {"entities_admitted": 61, "relationships_admitted": 29},
        "store_write_summary": {
            "weaviate": {"status": "SUCCEEDED", "chunks_written": 24},
            "neo4j": {
                "status": "SUCCEEDED", "nodes_written": 61, "relationships_written": 29,
                "verification_status": "VERIFIED",
            },
        },
        # Domain-wide totals must never be presented as this run's write results.
        "store_actual": {"neo4j_nodes": 1102, "neo4j_relationships": 2936},
        "scope": {
            "customer_name": "Sample Customer", "industry_id": "banking",
            "domain_id": "corporate_commercial", "product_line_name": "Letters of Credit",
        },
        "job_details": {
            "run_id": "run-123", "ingestion_batch_id": "batch-123",
            "hitl_summary": {"pending_decisions": 2, "approved_decisions": 1, "rejected_decisions": 1},
            "stages": [{"stage_id": "run.verify_report", "status": "RUNNING"}],
        },
    }

    path = Path(UnifiedIngestionReportGenerator().generate(
        domain="corporate_commercial", timestamp="2026-10-14T10:18:00Z",
        job_id="ingest-corporate_commercial-abc", run_summary=summary,
        document_metrics=[], semantic_html="", audit_html="", kg_html="",
        json_report_path=None, log_file_path=None, output_dir=tmp_path,
    ))
    report = path.read_text(encoding="utf-8")

    assert path.name == "ingest-corporate_commercial-abc.html"
    assert "Knowledge made useful" in report
    assert "61 nodes · 29 relationships" in report
    assert "1,102" not in report
    assert "2 items await reviewer attention" in report
    assert "Knowledge context locked" not in report  # Only supplied persisted stages are shown.
    assert "Run evidence ready" in report


def test_unified_report_escapes_untrusted_scope_and_source_labels(tmp_path):
    """Customer-provided labels are rendered as text rather than executable markup."""
    path = UnifiedIngestionReportGenerator().generate(
        domain="cards", timestamp="now", job_id="job-1",
        run_summary={"status": "failed", "scope": {"customer_name": "<script>alert(1)</script>", "domain_id": "cards"}},
        document_metrics=[{"document_path": "/secret/customer/source <file>.docx", "total_blocks": 2}],
        semantic_html="<script>bad()</script>", audit_html="", kg_html="",
        json_report_path=None, log_file_path=None, output_dir=tmp_path,
    )
    report = Path(path).read_text(encoding="utf-8")

    assert "<script>alert(1)</script>" not in report
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report
    assert "source &lt;file&gt;.docx" in report
    assert "/secret/customer/" not in report
    assert "bad()" not in report


def test_unified_report_labels_unavailable_neo4j_readback_as_writer_reported(tmp_path):
    """A successful writer call is not presented as verified when scoped read-back failed."""
    path = UnifiedIngestionReportGenerator().generate(
        domain="cards", timestamp="now", job_id="job-2",
        run_summary={
            "status": "success", "store_write_summary": {
                "weaviate": {"status": "SUCCEEDED", "chunks_written": 1},
                "neo4j": {
                    "status": "SUCCEEDED", "nodes_written": 2,
                    "relationships_written": 1, "verification_status": "UNAVAILABLE",
                },
            },
        },
        document_metrics=[], semantic_html="", audit_html="", kg_html="",
        json_report_path=None, log_file_path=None, output_dir=tmp_path,
    )
    report = Path(path).read_text(encoding="utf-8")

    assert "Writer-reported" in report
    assert "scoped read-back unavailable" in report
    assert "Reconciliation needs attention" in report


def test_unified_report_renders_submitter_and_append_only_reviewer_actions(tmp_path):
    """The downloadable report attributes submission, decisions, and graph writes."""
    generator = UnifiedIngestionReportGenerator()
    path = Path(generator.generate(
        domain="cards", timestamp="generation-time", job_id="ingest-audit-1",
        run_summary={
            "status": "COMPLETED_WITH_REVIEW_PENDING",
            "hitl_mode": "review_ambiguous",
            "scope": {"customer_id": "cust-1", "domain_id": "cards", "product_line_id": "pl-1"},
            "job_details": {
                "submitted_by": "submitter@example.test", "auth_source": "entra",
                "hitl_summary": {"pending_decisions": 1, "approved_decisions": 0, "rejected_decisions": 0},
            },
        },
        document_metrics=[], semantic_html="", audit_html="", kg_html="",
        json_report_path=None, log_file_path=None, output_dir=tmp_path,
    ))

    assert generator.refresh_audit_sections(
        path,
        {
            "job_id": "ingest-audit-1", "status": "COMPLETED", "submitted_by": "submitter@example.test",
            "auth_source": "entra", "customer_id": "cust-1", "tenant_id": "tenant-1",
            "industry_id": "banking", "domain_id": "cards", "product_line_id": "pl-1",
            "hitl_mode": "review_ambiguous", "hitl_summary": {
                "pending_decisions": 0, "approved_decisions": 1, "rejected_decisions": 0,
                "pending_writes": 0,
            },
        },
        [{
            "review_id": "rv-1", "short_summary": "Card authorization workflow",
            "event_type": "REVIEW_DECISION", "action": "APPROVE_AS_NEW",
            "status_after": "APPROVED", "reviewer_id": "reviewer@example.test",
            "created_at": "2026-10-14T11:30:00+00:00", "current_write_status": "SUCCEEDED",
            "current_write_result": {"nodes": 1, "relationships": 0},
        }, {
            "review_id": "rv-1", "short_summary": "Card authorization workflow",
            "event_type": "WRITE_EXECUTION", "action": "APPROVE_AS_NEW",
            "status_after": "APPROVED", "reviewer_id": "system:hitl-write-executor",
            "created_at": "2026-10-14T11:30:02+00:00",
            "event_details_json": {"write_status": "SUCCEEDED", "write_result": {"nodes": 1}},
        }],
        "2026-10-14T11:31:00+00:00",
    )
    report = path.read_text(encoding="utf-8")

    assert "submitter@example.test" in report
    assert "entra" in report
    assert "reviewer@example.test" in report
    assert "APPROVE AS NEW" in report
    assert "Card authorization workflow" in report
    assert "SUCCEEDED · nodes: 1" in report
    assert "2026-10-14T11:31:00+00:00" in report
    assert "No reviewer actions recorded" not in report


def test_report_refresh_escapes_reviewer_supplied_values(tmp_path):
    """Reviewer and review labels remain inert text in the shareable HTML artifact."""
    generator = UnifiedIngestionReportGenerator()
    path = Path(generator.generate(
        domain="cards", timestamp="now", job_id="ingest-audit-safe",
        run_summary={"status": "success"}, document_metrics=[], semantic_html="",
        audit_html="", kg_html="", json_report_path=None, log_file_path=None,
        output_dir=tmp_path,
    ))
    assert generator.refresh_audit_sections(
        path,
        {"job_id": "ingest-audit-safe", "status": "COMPLETED", "submitted_by": "<script>bad()</script>",
         "domain_id": "cards", "product_line_id": "pl-1", "hitl_summary": {"approved_decisions": 1}},
        [{"review_id": "rv-2", "short_summary": "<img src=x onerror=bad()>", "action": "APPROVE_AS_NEW",
          "status_after": "APPROVED", "reviewer_id": "<script>evil()</script>",
          "created_at": "today"}],
        "now",
    )
    report = path.read_text(encoding="utf-8")
    assert "<script>bad()</script>" not in report
    assert "&lt;script&gt;bad()&lt;/script&gt;" in report
    assert "<img src=x onerror=bad()>" not in report
    assert "&lt;img src=x onerror=bad()&gt;" in report

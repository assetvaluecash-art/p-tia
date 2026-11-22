"""English acceptance checks for durable stage artifact checkpoint behavior."""
import pytest
from unittest.mock import Mock

from kbfabric_ingest.ingest.checkpoints import read_checkpoint_artifact, stable_digest, write_checkpoint_artifact
from kbfabric_ingest.ingest.ingestion_metadata import scoped_block_id, scoped_entity_id
from kbfabric_ingest.ingest.models import BlockCategory, CanonicalBlock, EnrichedBlock
from scripts.run_ingestion_job import (
    JobProgressTracker,
    _metadata_values_as_raw,
    _should_apply_force_rewrite,
)


def test_checkpoint_artifact_round_trips_and_verifies_integrity(tmp_path):
    """A completed stage output survives worker restart as a verified JSON artifact."""
    artifact = tmp_path / "run-1" / "lane2.pass2.json"
    payload = {"block_kind": "enriched", "blocks": [{"block_id": "b-1", "text": "content"}]}

    digest = write_checkpoint_artifact(artifact, stage_id="lane2.pass2", payload=payload)
    stage, restored = read_checkpoint_artifact(artifact, digest)

    assert stage == "lane2.pass2"
    assert restored == payload


def test_checkpoint_artifact_rejects_tampering(tmp_path):
    """A changed artifact cannot be treated as the output of a completed stage."""
    artifact = tmp_path / "run-1" / "blocks.prepared.json"
    digest = write_checkpoint_artifact(
        artifact, stage_id="blocks.prepared", payload={"block_kind": "canonical", "blocks": []}
    )
    artifact.write_text('{"schema_version":1,"stage_id":"blocks.prepared","payload":{}}')

    with pytest.raises(ValueError, match="integrity"):
        read_checkpoint_artifact(artifact, digest)


def test_enriched_checkpoint_preserves_typed_blocks_and_binary_images(tmp_path):
    """A resume artifact reconstructs Pydantic block types, including image bytes."""
    canonical = CanonicalBlock(
        block_id="block-1", source_file="spec.pdf", file_type="pdf", domain="banking",
        page_number=1, block_index=0, block_type="image", text="diagram",
        image_bytes=b"\x00\x01image", metadata={"source": "golden"},
    )
    enriched = EnrichedBlock(
        block=canonical, block_category=list(BlockCategory)[0], kg_node_type=None,
        candidate_entities=[], candidate_relationships=[], embedding=[0.1, 0.2],
        pii_spans=[], original_text_redacted="diagram", masked_text="diagram",
    )
    serialized = __import__("json").loads(enriched.model_dump_json())
    artifact = tmp_path / "run-1" / "lane2.pass2.json"
    digest = write_checkpoint_artifact(
        artifact, stage_id="lane2.pass2",
        payload={"block_kind": "enriched", "blocks": [serialized]},
    )

    stage, payload = read_checkpoint_artifact(artifact, digest)
    restored = EnrichedBlock.model_validate(payload["blocks"][0])

    assert stage == "lane2.pass2"
    assert isinstance(restored, EnrichedBlock)
    assert isinstance(restored.block, CanonicalBlock)
    assert restored.block.image_bytes == b"\x00\x01image"
    assert restored.embedding == [0.1, 0.2]


def test_worker_restore_loads_latest_checkpoint_when_provenance_matches(tmp_path):
    """A restarted worker reconstructs saved extraction output after validating source/context."""
    source_manifest = {"inventory_complete": True, "files": [{"name": "spec.pdf", "sha256": "abc"}]}
    context = {"customer_id": "customer-1", "domain_id": "cards", "version": 3}
    block = CanonicalBlock(
        block_id="block-1", source_file="spec.pdf", file_type="pdf", domain="cards",
        page_number=1, block_index=0, block_type="paragraph", text="requirement",
        image_bytes=None, metadata={},
    )
    enriched = EnrichedBlock(
        block=block, block_category=list(BlockCategory)[0], candidate_entities=[],
        candidate_relationships=[], embedding=[0.1], pii_spans=[],
        original_text_redacted="requirement", masked_text="requirement",
    )
    run_id = "run-1"
    artifact = tmp_path / "checkpoints" / run_id / "lane2.pass2-attempt-1.json"
    digest = write_checkpoint_artifact(
        artifact, stage_id="lane2.pass2",
        payload={"block_kind": "enriched", "blocks": [__import__("json").loads(enriched.model_dump_json())]},
    )
    repository = Mock()
    repository.get_resume_record.return_value = {
        "source_manifest_json": source_manifest, "run_context_json": context,
    }
    repository.get_latest_checkpoint.return_value = {
        "stage_id": "lane2.pass2", "artifact_uri": str(artifact),
        "artifact_sha256": digest,
        "source_manifest_sha256": stable_digest(source_manifest),
        "context_sha256": stable_digest(context),
    }
    tracker = JobProgressTracker.__new__(JobProgressTracker)
    tracker.job_id = "job-1"
    tracker.progress_dir = tmp_path / "progress"
    tracker.state = {"run_id": run_id, "audit_context": {"execution_attempt": 2}}
    tracker._run_repository = repository

    restored = tracker.restore_latest_blocks_checkpoint()

    assert restored["stage_id"] == "lane2.pass2"
    assert isinstance(restored["enriched_blocks"][0], EnrichedBlock)
    assert restored["enriched_blocks"][0].block.text == "requirement"


def test_worker_restores_report_inputs_without_loading_or_replaying_stores(tmp_path):
    """A report recovery attempt restores report inputs from the verified shared artifact."""
    source_manifest = {"inventory_complete": True, "files": [{"name": "spec.pdf", "sha256": "abc"}]}
    context = {"customer_id": "customer-1", "domain_id": "cards", "version": 3}
    run_id = "run-report"
    artifact = tmp_path / "checkpoints" / run_id / "stores.write-attempt-1.json"
    payload = {
        "payload_kind": "report_inputs", "domain": "cards", "timestamp": "20261013",
        "run_summary": {"weaviate_chunks": 4}, "document_metrics": [],
        "semantic_html": "<p>semantic</p>", "audit_html": "<p>audit</p>",
        "kg_html": "<p>graph</p>", "json_report_path": "reports/run.json",
        "log_file_path": "logs/run.log",
    }
    digest = write_checkpoint_artifact(artifact, stage_id="stores.write", payload=payload)
    repository = Mock()
    repository.get_resume_record.return_value = {
        "source_manifest_json": source_manifest, "run_context_json": context,
    }
    repository.get_latest_checkpoint.return_value = {
        "stage_id": "stores.write", "artifact_uri": str(artifact),
        "artifact_sha256": digest,
        "source_manifest_sha256": stable_digest(source_manifest),
        "context_sha256": stable_digest(context),
    }
    tracker = JobProgressTracker.__new__(JobProgressTracker)
    tracker.job_id = "job-report"
    tracker.progress_dir = tmp_path / "progress"
    tracker.state = {"run_id": run_id, "audit_context": {"execution_attempt": 2}}
    tracker._run_repository = repository

    restored = tracker.restore_latest_blocks_checkpoint()

    assert restored["stage_id"] == "stores.write"
    assert restored["report_inputs"] == payload


def test_report_only_recovery_calls_unified_generator_without_store_writers():
    """Report recovery consumes persisted report inputs and does not invoke either store writer."""
    from kbfabric_ingest.ingest.ingestion_service import IngestionService

    service = IngestionService.__new__(IngestionService)
    service.unified_report_generator = Mock()
    service.unified_report_generator.generate.return_value = "reports/job-1.html"
    service.weaviate_writer = Mock()
    service.kg_writer = Mock()
    payload = {
        "payload_kind": "report_inputs", "domain": "cards", "timestamp": "20261013",
        "job_id": "job-1", "run_summary": {"weaviate_chunks": 4},
        "document_metrics": [], "semantic_html": "<p>semantic</p>",
        "audit_html": "<p>audit</p>", "kg_html": "<p>graph</p>",
        "json_report_path": "reports/run.json", "log_file_path": "logs/run.log",
    }

    result = service.resume_report_from_checkpoint(payload)

    assert result == {"report_paths": {"unified": "reports/job-1.html"}, "error": None}
    service.unified_report_generator.generate.assert_called_once()
    service.weaviate_writer.write_blocks.assert_not_called()
    service.kg_writer.write_blocks.assert_not_called()


def test_store_replay_keys_are_deterministic_within_customer_scope():
    """Replaying a saved admission plan targets the same vector and graph identities."""
    scope = "tenant:tenant-1|customer:customer-1"

    assert scoped_block_id(scope, "source-block-1") == scoped_block_id(scope, "source-block-1")
    assert scoped_entity_id(scope, "cards", "Requirement", "card activation") == scoped_entity_id(
        scope, "cards", "Requirement", "card activation"
    )
    assert scoped_block_id(scope, "source-block-1") != scoped_block_id(
        "tenant:tenant-1|customer:customer-2", "source-block-1"
    )


@pytest.mark.parametrize(
    "enabled, attempt, expected",
    [(True, 1, True), (True, 2, False), (False, 1, False)],
)
def test_force_rewrite_runs_only_before_the_first_attempt(enabled, attempt, expected):
    """Recovering a job cannot repeat the product-line cleanup action."""
    assert _should_apply_force_rewrite(enabled, attempt) is expected


def test_submission_metadata_is_restored_to_ingestion_service_raw_format():
    """Resume/worker metadata must reach run_ingestion's semicolon-delimited contract."""
    assert _metadata_values_as_raw(["PRJ-1", " PRJ-2 "]) == "PRJ-1;PRJ-2"
    assert _metadata_values_as_raw(["system=core", "region=APAC"]) == "system=core;region=APAC"
    assert _metadata_values_as_raw([]) is None
    assert _metadata_values_as_raw("legacy;value") == "legacy;value"

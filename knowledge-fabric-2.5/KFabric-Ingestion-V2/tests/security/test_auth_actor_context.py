"""Tests for resolving authenticated actor provenance."""

from fastapi import HTTPException
import jwt
import pytest

from kbfabric_ingest.security.auth import AuthenticatedActor, require_authenticated_actor
from kbfabric_ingest.scheduler import api


def test_dev_test_token_is_marked_as_nonproduction_identity():
    """Objective: verify accepted dev tokens remain distinguishable in audit evidence.

    This matters because ingestion audit records must identify the actual authentication path.
    Invariant: a test token resolves to its test actor ID and is never labelled as a Kong JWT.
    """
    actor = require_authenticated_actor("Bearer test-ingestion-actor")

    assert actor.user_id == "test-ingestion-actor"
    assert actor.auth_source == "dev_test_token"


def test_kong_jwt_permissions_are_exposed_on_authenticated_actor():
    """Objective: verify the trusted gateway permission claim reaches the actor used by the rewrite gate.

    This matters because privileged maintenance actions must be authorized from identity evidence.
    Invariant: only the permissions claim from a non-test JWT is copied into actor permissions.
    """
    token = jwt.encode({"userId": "operator-1", "permissions": ["ingestion:force_rewrite"]}, "test-signing-key-long-enough-for-hs256")

    actor = require_authenticated_actor(f"Bearer {token}")

    assert actor.user_id == "operator-1"
    assert actor.auth_source == "kong_jwt"
    assert actor.permissions == frozenset({"ingestion:force_rewrite"})


def test_missing_authorization_is_rejected_for_authenticated_actor():
    """Objective: verify ingestion cannot create an actor audit record without identity evidence.

    This matters because submitted_by must come from the trusted auth dependency, not the request body.
    Invariant: missing Authorization fails closed with HTTP 401.
    """
    with pytest.raises(HTTPException) as exc_info:
        require_authenticated_actor(None)

    assert exc_info.value.status_code == 401


def test_ingestion_run_audit_contract_captures_resolved_actor_and_frozen_scope():
    """Objective: verify submitted actor and the resolved customer ontology scope enter the audit record.

    This matters because job progress and downstream batch provenance must be attributable to the identity and ontology context selected at submission.
    Invariant: actor identity/source and frozen customer, domain, product-line, and ontology versions are copied from trusted server-side inputs.
    """
    actor = AuthenticatedActor(user_id="reviewer-17", auth_source="kong_jwt")
    frozen_context = {
        "customer_id": "customer-a",
        "tenant_id": "tenant-a",
        "industry_id": "banking",
        "domain_id": "consumer_retail",
        "product_line_id": "DOMAIN:consumer_retail|PRODUCT_LINE:Retail Cards",
        "customer_ontology_id": "co-7",
        "customer_ontology_version": 4,
        "canonical_ontology_id": "banking",
        "canonical_ontology_version": "5.0.1",
        "extraction_policy_version": "5.0.1",
    }

    record = api._ingestion_run_record(
        run_id="run-1",
        job_id="ingest-1",
        actor=actor,
        frozen_context=frozen_context,
        domain_id="consumer_retail",
        product_line_id="DOMAIN:consumer_retail|PRODUCT_LINE:Retail Cards",
        document_nature="qe_assets",
        hitl_mode="review_ambiguous",
        force_rewrite_requested=False,
        submission_metadata={
            "project_ids": ["project-a", "project-b"],
            "context_tags": ["system=core", "region=APAC"],
        },
    )

    assert record["submitted_by"] == "reviewer-17"
    assert record["auth_source"] == "kong_jwt"
    assert record["run_context"] == frozen_context
    assert record["customer_ontology_version"] == 4
    assert record["product_line_id"].endswith("Retail Cards")
    assert record["submission_metadata"] == {
        "project_ids": ["project-a", "project-b"],
        "context_tags": ["system=core", "region=APAC"],
    }


def test_submission_metadata_is_attached_to_initial_and_resumed_worker_context():
    """Both fresh dispatch and resume use the same metadata-bearing audit context helper."""
    metadata = {"project_ids": ["project-a"], "context_tags": ["system=core"]}
    base = {"run_id": "run-1", "execution_attempt": 1}

    initial = api._attach_submission_metadata(base, metadata)
    resumed = api._attach_submission_metadata(
        {"run_id": "run-1", "execution_attempt": 2}, metadata
    )

    assert initial["submission_metadata"] == metadata
    assert resumed["submission_metadata"] == metadata
    assert "submission_metadata" not in base


def test_job_submission_openapi_exposes_authorization_header():
    """Objective: verify ingestion job submission advertises its Authorization header contract.

    This matters because the UI must send the identity token used to attribute the job.
    Invariant: both directory and upload submission routes declare Authorization as a request header.
    """
    schema = api.app.openapi()
    for path in ("/jobs", "/jobs/upload"):
        parameters = schema["paths"][path]["post"].get("parameters", [])
        assert any(p.get("in") == "header" and p.get("name", "").lower() == "authorization" and p.get("required") is True for p in parameters)

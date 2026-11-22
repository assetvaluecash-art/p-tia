from __future__ import annotations

import json
import os
import uuid
from typing import Any

import psycopg

from kbfabric_ingest.ingest.execution_plan import ExecutionPlan
from kbfabric_ingest.utils.logger import get_logger


class HITLReviewRepository:
    """PostgreSQL persistence for execution plans requiring human review."""

    def __init__(self) -> None:
        self.logger = get_logger(__name__)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ["POSTGRES_PORT"]),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
        )

    @staticmethod
    def _append_run_activity(
        cur: Any, *, ingestion_batch_id: str, event_type: str,
        status: str, payload: dict[str, Any], refresh_review_status: bool = False,
    ) -> None:
        """Mirror safe HITL lifecycle facts into the matching ingestion event stream."""
        cur.execute(
            """INSERT INTO ingestion_run_event (
                   event_id, run_id, job_id, event_type, status, payload_json
               )
               SELECT %s, run_id, job_id, %s, %s, %s::jsonb
                 FROM ingestion_run WHERE ingestion_batch_id = %s""",
            (str(uuid.uuid4()), event_type, status, json.dumps(payload), ingestion_batch_id),
        )
        if refresh_review_status:
            cur.execute(
                """UPDATE ingestion_run AS r
                      SET status = CASE
                            WHEN EXISTS (
                                SELECT 1 FROM hitl_review h
                                 WHERE h.ingestion_batch_id = r.ingestion_batch_id
                                   AND h.status = 'PENDING'
                            ) THEN 'COMPLETED_WITH_REVIEW_PENDING'
                            ELSE 'COMPLETED'
                          END,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE r.ingestion_batch_id = %s
                      AND r.status IN ('COMPLETED', 'COMPLETED_WITH_REVIEW_PENDING')
                      AND r.status <> CASE
                            WHEN EXISTS (
                                SELECT 1 FROM hitl_review h
                                 WHERE h.ingestion_batch_id = r.ingestion_batch_id
                                   AND h.status = 'PENDING'
                            ) THEN 'COMPLETED_WITH_REVIEW_PENDING'
                            ELSE 'COMPLETED'
                          END""",
                (ingestion_batch_id,),
            )

    def list_pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return pending review items ordered by priority and creation time."""
        if limit < 1:
            raise ValueError("limit must be >= 1")

        self.logger.info(
            "hitl.review.list_pending.start",
            limit=limit,
        )

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        review_id,
                        ingestion_batch_id,
                        plan_id,
                        queue_type,
                        status,
                        priority,
                        reason_code,
                        admission_behavior,
                        short_summary,
                        decision_question,
                        source_temporary_id,
                        source_context_json,
                        document_context_json,
                        ontology_context_json,
                        mapping_context_json,
                        context_resolution_json,
                        relationship_evidence_json,
                        provenance_json,
                        review_payload_json,
                        execution_plan_json,
                        created_at,
                        updated_at
                    FROM hitl_review
                    WHERE status = 'PENDING'
                    ORDER BY
                        CASE priority
                            WHEN 'CRITICAL' THEN 1
                            WHEN 'HIGH' THEN 2
                            WHEN 'NORMAL' THEN 3
                            WHEN 'LOW' THEN 4
                            ELSE 5
                        END,
                        created_at ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                columns = [desc.name for desc in cur.description]
                rows = [
                    dict(zip(columns, row))
                    for row in cur.fetchall()
                ]

        self.logger.info(
            "hitl.review.list_pending.completed",
            count=len(rows),
            limit=limit,
        )
        return rows

    def list_scoped(
        self,
        *,
        ingestion_batch_id: str,
        domain_id: str,
        product_line_id: str,
        status: str | None = None,
        queue_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """List review items within the required batch/domain/product-line scope."""
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        filters = [
            "ingestion_batch_id = %(batch_id)s",
            "ontology_context_json->>'domain_id' = %(domain_id)s",
            "ontology_context_json->>'product_line_id' = %(product_line_id)s",
        ]
        params: dict[str, Any] = {
            "batch_id": ingestion_batch_id,
            "domain_id": domain_id,
            "product_line_id": product_line_id,
            "limit": limit,
            "offset": offset,
        }
        if status is not None:
            filters.append("status = %(status)s")
            params["status"] = status
        if queue_type is not None:
            filters.append("queue_type = %(queue_type)s")
            params["queue_type"] = queue_type

        where_clause = " AND ".join(filters)
        query = f"""
            SELECT review_id, ingestion_batch_id, plan_id, queue_type, status,
                   priority, reason_code, admission_behavior, short_summary,
                   decision_question, source_temporary_id, source_context_json,
                   document_context_json, ontology_context_json, mapping_context_json,
                   context_resolution_json, relationship_evidence_json, provenance_json,
                   review_payload_json, execution_plan_json, resolved_plan_json,
                   reviewer_action, reviewer_id, reviewer_notes, write_status, write_result_json, write_error, write_attempts, created_at,
                   updated_at, resolved_at
            FROM hitl_review
            WHERE {where_clause}
            ORDER BY created_at, review_id
            LIMIT %(limit)s OFFSET %(offset)s
        """
        count_query = f"SELECT COUNT(*) FROM hitl_review WHERE {where_clause}"
        count_params = {key: value for key, value in params.items() if key not in {"limit", "offset"}}

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(count_query, count_params)
                total = int(cur.fetchone()[0])
                cur.execute(query, params)
                columns = [desc.name for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
        return rows, total

    def get_scoped(
        self,
        *,
        review_id: str,
        ingestion_batch_id: str,
        domain_id: str,
        product_line_id: str,
    ) -> dict[str, Any] | None:
        """Read one review only when all caller-supplied run scopes match."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT review_id, ingestion_batch_id, plan_id, queue_type, status,
                           priority, reason_code, admission_behavior, short_summary,
                           decision_question, source_temporary_id, source_context_json,
                           document_context_json, ontology_context_json, mapping_context_json,
                           context_resolution_json, relationship_evidence_json, provenance_json,
                           review_payload_json, execution_plan_json, resolved_plan_json,
                           reviewer_action, reviewer_id, reviewer_notes, write_status, write_result_json, write_error, write_attempts, created_at,
                           updated_at, resolved_at
                    FROM hitl_review
                    WHERE review_id = %(review_id)s
                      AND ingestion_batch_id = %(batch_id)s
                      AND ontology_context_json->>'domain_id' = %(domain_id)s
                      AND ontology_context_json->>'product_line_id' = %(product_line_id)s
                    """,
                    {
                        "review_id": review_id,
                        "batch_id": ingestion_batch_id,
                        "domain_id": domain_id,
                        "product_line_id": product_line_id,
                    },
                )
                row = cur.fetchone()
                if row is None:
                    return None
                columns = [desc.name for desc in cur.description]
        return dict(zip(columns, row))

    def list_history(
        self,
        *,
        review_id: str,
        ingestion_batch_id: str,
        domain_id: str,
        product_line_id: str,
    ) -> list[dict[str, Any]]:
        """List append-only reviewer events for a scoped review item."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_id, review_id, event_type, action, status_before,
                           status_after, reviewer_id, reviewer_notes, resolved_plan_json,
                           event_details_json, created_at
                    FROM hitl_review_event
                    WHERE review_id = %(review_id)s
                      AND EXISTS (
                          SELECT 1 FROM hitl_review r
                          WHERE r.review_id = hitl_review_event.review_id
                            AND r.ingestion_batch_id = %(batch_id)s
                            AND r.ontology_context_json->>'domain_id' = %(domain_id)s
                            AND r.ontology_context_json->>'product_line_id' = %(product_line_id)s
                      )
                    ORDER BY created_at, event_id
                    """,
                    {
                        "review_id": review_id,
                        "batch_id": ingestion_batch_id,
                        "domain_id": domain_id,
                        "product_line_id": product_line_id,
                    },
                )
                columns = [desc.name for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def list_report_audit(
        self, *, ingestion_batch_id: str, domain_id: str, product_line_id: str,
    ) -> list[dict[str, Any]]:
        """Return append-only reviewer/write events for one frozen report scope."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT e.event_id, e.review_id, r.queue_type, r.short_summary,
                           e.event_type, e.action, e.status_after, e.reviewer_id,
                           e.created_at, e.event_details_json,
                           r.write_status AS current_write_status,
                           r.write_result_json AS current_write_result
                    FROM hitl_review_event e
                    JOIN hitl_review r ON r.review_id = e.review_id
                    WHERE r.ingestion_batch_id = %(batch_id)s
                      AND r.ontology_context_json->>'domain_id' = %(domain_id)s
                      AND r.ontology_context_json->>'product_line_id' = %(product_line_id)s
                      AND e.reviewer_id IS NOT NULL
                      AND e.reviewer_id <> ''
                    ORDER BY e.created_at, e.event_id
                    """,
                    {"batch_id": ingestion_batch_id, "domain_id": domain_id,
                     "product_line_id": product_line_id},
                )
                columns = [desc.name for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def record_decision(
        self,
        *,
        review_id: str,
        ingestion_batch_id: str,
        domain_id: str,
        product_line_id: str,
        reviewer_action: str,
        reviewer_id: str,
        reviewer_notes: str | None = None,
        resolved_plan: ExecutionPlan | None = None,
    ) -> dict[str, Any]:
        """Atomically persist the latest decision and append its audit event.

        This commits review state and audit history before the API starts Neo4j execution.
        """
        allowed_actions = {
            "APPROVE_REUSE", "APPROVE_REUSE_WITH_ALIAS",
            "APPROVE_AS_NEW", "REJECT", "LEAVE_UNRESOLVED",
        }
        if reviewer_action not in allowed_actions:
            raise ValueError(f"Unsupported reviewer action: {reviewer_action}")
        if reviewer_action in {"APPROVE_REUSE", "APPROVE_REUSE_WITH_ALIAS", "APPROVE_AS_NEW"} and resolved_plan is None:
            raise ValueError(f"{reviewer_action} requires a resolved execution plan")

        next_status = (
            "PENDING" if reviewer_action == "LEAVE_UNRESOLVED"
            else "REJECTED" if reviewer_action == "REJECT"
            else "APPROVED"
        )
        plan_json = (
            json.dumps(resolved_plan.model_dump(mode="json"))
            if resolved_plan is not None else None
        )
        event_id = str(uuid.uuid4())

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE hitl_review
                    SET status = %(status)s, reviewer_action = %(action)s,
                        reviewer_id = %(reviewer_id)s, reviewer_notes = %(notes)s,
                        resolved_plan_json = %(resolved_plan)s::jsonb,
                        write_status = CASE WHEN %(resolved_plan)s::jsonb IS NULL THEN 'NOT_REQUIRED' ELSE 'PENDING' END,
                        write_result_json = '{}'::jsonb, write_error = NULL, write_attempts = 0, write_started_at = NULL,
                        updated_at = CURRENT_TIMESTAMP,
                        resolved_at = CASE WHEN %(status)s = 'PENDING' THEN NULL ELSE CURRENT_TIMESTAMP END
                    WHERE review_id = %(review_id)s
                      AND ingestion_batch_id = %(batch_id)s
                      AND ontology_context_json->>'domain_id' = %(domain_id)s
                      AND ontology_context_json->>'product_line_id' = %(product_line_id)s
                      AND status = 'PENDING'
                    RETURNING status, updated_at, resolved_at
                    """,
                    {
                        "status": next_status, "action": reviewer_action,
                        "reviewer_id": reviewer_id, "notes": reviewer_notes,
                        "resolved_plan": plan_json, "review_id": review_id,
                        "batch_id": ingestion_batch_id, "domain_id": domain_id,
                        "product_line_id": product_line_id,
                    },
                )
                updated = cur.fetchone()
                if updated is None:
                    raise ValueError("Pending HITL review not found in the supplied scope")
                cur.execute(
                    """
                    INSERT INTO hitl_review_event (
                        event_id, review_id, event_type, action, status_before,
                        status_after, reviewer_id, reviewer_notes, resolved_plan_json
                    ) VALUES (%s, %s, 'REVIEW_DECISION', %s, 'PENDING', %s, %s, %s, %s::jsonb)
                    """,
                    (event_id, review_id, reviewer_action, next_status, reviewer_id, reviewer_notes, plan_json),
                )
                self._append_run_activity(
                    cur, ingestion_batch_id=ingestion_batch_id,
                    event_type="hitl.review_decided", status=next_status,
                    payload={"review_id": review_id, "action": reviewer_action,
                             "reviewer_id": reviewer_id},
                    refresh_review_status=True,
                )

        status, updated_at, resolved_at = updated
        return {
            "review_id": review_id, "status": status,
            "reviewer_action": reviewer_action, "reviewer_id": reviewer_id,
            "reviewer_notes": reviewer_notes, "updated_at": updated_at,
            "resolved_at": resolved_at, "event_id": event_id,
            "execution_status": "PENDING" if resolved_plan is not None else "NOT_REQUIRED",
        }

    def claim_write(self, *, review_id: str, ingestion_batch_id: str, domain_id: str, product_line_id: str) -> bool:
        """Claim an approved, retryable write so concurrent requests cannot execute it twice."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE hitl_review SET write_status = 'IN_PROGRESS', write_started_at = CURRENT_TIMESTAMP,
                        write_attempts = write_attempts + 1, write_error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE review_id = %(review_id)s AND ingestion_batch_id = %(batch_id)s
                      AND ontology_context_json->>'domain_id' = %(domain_id)s
                      AND ontology_context_json->>'product_line_id' = %(product_line_id)s
                      AND status = 'APPROVED' AND (write_status IN ('PENDING', 'FAILED') OR (write_status = 'IN_PROGRESS' AND write_started_at < CURRENT_TIMESTAMP - INTERVAL '5 minutes'))
                    RETURNING review_id
                    """,
                    {"review_id": review_id, "batch_id": ingestion_batch_id,
                     "domain_id": domain_id, "product_line_id": product_line_id},
                )
                return cur.fetchone() is not None

    def complete_write(
        self, *, review_id: str, ingestion_batch_id: str, domain_id: str,
        product_line_id: str, result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Persist the latest idempotent KG write outcome for UI and retry visibility."""
        status = "FAILED" if error else "SUCCEEDED"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE hitl_review SET write_status = %(status)s, write_started_at = NULL,
                        write_result_json = %(result)s::jsonb, write_error = %(error)s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE review_id = %(review_id)s AND ingestion_batch_id = %(batch_id)s
                      AND ontology_context_json->>'domain_id' = %(domain_id)s
                      AND ontology_context_json->>'product_line_id' = %(product_line_id)s
                      AND status = 'APPROVED'
                    RETURNING reviewer_action, write_attempts
                    """,
                    {"status": status, "result": json.dumps(result or {}), "error": error,
                     "review_id": review_id, "batch_id": ingestion_batch_id,
                     "domain_id": domain_id, "product_line_id": product_line_id},
                )
                updated = cur.fetchone()
                if updated is None:
                    raise ValueError("Approved HITL item not found in supplied scope")
                reviewer_action, attempts = updated
                cur.execute(
                    """
                    INSERT INTO hitl_review_event (
                        event_id, review_id, event_type, action, status_before,
                        status_after, reviewer_id, reviewer_notes, event_details_json
                    ) VALUES (%s, %s, 'WRITE_EXECUTION', %s, 'APPROVED', 'APPROVED',
                              'system:hitl-write-executor', %s, %s::jsonb)
                    """,
                    (str(uuid.uuid4()), review_id, reviewer_action, error,
                     json.dumps({"write_status": status, "write_attempt": attempts,
                                 "write_result": result or {}, "write_error": error})),
                )
                self._append_run_activity(
                    cur, ingestion_batch_id=ingestion_batch_id,
                    event_type="hitl.write_status_changed", status=status,
                    payload={"review_id": review_id, "write_attempt": attempts,
                             "write_result": result or {}, "error_present": bool(error)},
                )

    def list_by_ingestion_batch(
        self,
        ingestion_batch_id: str,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return HITL reviews belonging to one ingestion batch."""
        self.logger.info(
            "hitl.review.batch_list.start",
            ingestion_batch_id=ingestion_batch_id,
            status=status,
        )

        query = """
            SELECT
                review_id,
                ingestion_batch_id,
                plan_id,
                queue_type,
                status,
                priority,
                reason_code,
                admission_behavior,
                short_summary,
                decision_question,
                source_temporary_id,
                source_context_json,
                document_context_json,
                ontology_context_json,
                mapping_context_json,
                context_resolution_json,
                relationship_evidence_json,
                provenance_json,
                review_payload_json,
                execution_plan_json,
                reviewer_action,
                reviewer_id,
                reviewer_notes,
                resolved_plan_json,
                created_at,
                updated_at,
                resolved_at
            FROM hitl_review
            WHERE ingestion_batch_id = %(ingestion_batch_id)s
        """

        params: dict[str, Any] = {
            "ingestion_batch_id": ingestion_batch_id,
        }

        if status is not None:
            query += " AND status = %(status)s"
            params["status"] = status

        query += " ORDER BY created_at, review_id"

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
                columns = [desc.name for desc in cur.description]

        result = [dict(zip(columns, row)) for row in rows]

        self.logger.info(
            "hitl.review.batch_list.completed",
            ingestion_batch_id=ingestion_batch_id,
            status=status,
            review_count=len(result),
        )

        return result

    def enqueue(
        self,
        *,
        plan: ExecutionPlan,
        ingestion_batch_id: str | None = None,
        queue_type: str,
        reason_code: str,
        admission_behavior: str,
        priority: str = "NORMAL",
        short_summary: str | None = None,
        decision_question: str | None = None,
        source_context: dict[str, Any] | None = None,
        document_context: dict[str, Any] | None = None,
        ontology_context: dict[str, Any] | None = None,
        mapping_context: dict[str, Any] | None = None,
        relationship_evidence: list[dict[str, Any]] | None = None,
        review_payload: dict[str, Any] | None = None,
    ) -> str:
        """Persist a review item and return its review_id."""
        review_id = f"rv-{uuid.uuid4().hex[:12]}"
        plan_json = plan.model_dump(mode="json")

        context_resolutions = plan.context_resolutions or []
        provenance = plan.provenance or {}

        persisted_payload = {
            "plan_id": plan.plan_id,
            "action": plan.action.value,
            "reason_code": reason_code,
            "queue_type": queue_type,
            "admission_behavior": admission_behavior,
            **(review_payload or {}),
        }

        self.logger.info(
            "hitl.review.enqueue.start",
            review_id=review_id,
            ingestion_batch_id=ingestion_batch_id,
            plan_id=plan.plan_id,
            queue_type=queue_type,
            reason_code=reason_code,
        )

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO hitl_review (
                        review_id,
                        ingestion_batch_id,
                        plan_id,
                        queue_type,
                        status,
                        priority,
                        reason_code,
                        admission_behavior,
                        short_summary,
                        decision_question,
                        source_temporary_id,
                        source_context_json,
                        document_context_json,
                        ontology_context_json,
                        mapping_context_json,
                        context_resolution_json,
                        relationship_evidence_json,
                        provenance_json,
                        review_payload_json,
                        execution_plan_json
                    )
                    VALUES (
                        %(review_id)s,
                        %(ingestion_batch_id)s,
                        %(plan_id)s,
                        %(queue_type)s,
                        'PENDING',
                        %(priority)s,
                        %(reason_code)s,
                        %(admission_behavior)s,
                        %(short_summary)s,
                        %(decision_question)s,
                        %(source_temporary_id)s,
                        %(source_context)s::jsonb,
                        %(document_context)s::jsonb,
                        %(ontology_context)s::jsonb,
                        %(mapping_context)s::jsonb,
                        %(context_resolutions)s::jsonb,
                        %(relationship_evidence)s::jsonb,
                        %(provenance)s::jsonb,
                        %(review_payload)s::jsonb,
                        %(execution_plan)s::jsonb
                    )
                    ON CONFLICT (plan_id)
                    DO UPDATE SET
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING review_id
                    """,
                    {
                        "review_id": review_id,
                        "ingestion_batch_id": ingestion_batch_id,
                        "plan_id": plan.plan_id,
                        "queue_type": queue_type,
                        "priority": priority,
                        "reason_code": reason_code,
                        "admission_behavior": admission_behavior,
                        "short_summary": short_summary,
                        "decision_question": decision_question,
                        "source_temporary_id": (
                            plan.execution_metadata.get("source_temporary_id")
                            or plan.provenance.get("source_temporary_id")
                        ),
                        "source_context": json.dumps(source_context or {}),
                        "document_context": json.dumps(document_context or {}),
                        "ontology_context": json.dumps(ontology_context or {}),
                        "mapping_context": json.dumps(mapping_context or {}),
                        "context_resolutions": json.dumps(context_resolutions),
                        "relationship_evidence": json.dumps(
                            relationship_evidence or []
                        ),
                        "provenance": json.dumps(provenance),
                        "review_payload": json.dumps(persisted_payload),
                        "execution_plan": json.dumps(plan_json),
                    },
                )
                persisted_review_id = cur.fetchone()[0]

        self.logger.info(
            "hitl.review.enqueue.completed",
            review_id=persisted_review_id,
            ingestion_batch_id=ingestion_batch_id,
            plan_id=plan.plan_id,
            queue_type=queue_type,
            status="PENDING",
        )

        return persisted_review_id

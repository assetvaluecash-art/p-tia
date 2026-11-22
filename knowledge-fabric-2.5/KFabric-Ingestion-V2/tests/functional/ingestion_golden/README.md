# Ingestion golden functional journeys

This suite is for real API journeys through onboarding, industry/domain/product-line contextualization, submission, all 15 business stages, event replay, checkpoints, store writes, HITL review, and reports. Each assertion is persisted independently in JSONL with run ID, case ID, HTTP method/path/status, expected result, actual result, timestamps, duration, and source touchpoints. The JSON summary is derived from the JSONL evidence and must never replace it.

Functional tests use uniquely named QA customers and scoped batches. They must not deactivate customers or clear a shared domain/store. Test all three industry policies and review modes. Pairwise combinations are appropriate for metadata variants; each review action, authorization boundary, stage transition, checkpoint restore, and store reconciliation boundary requires a direct assertion.

Golden assets belong under `assets/`. A run manifest identifies the selected industry/domain/product line and the exact asset hashes. Generated service responses and store evidence belong under `reports/functional/<run-id>/` and are immutable after the run completes; reruns receive a new run ID.

## Full live matrix

`run_industry_policy_matrix.py` is the exhaustive real API journey runner. For each industry it creates one fresh QA customer, configures two domains/product lines on that same customer, then submits each ten-page asset under `review_all`, `review_ambiguous`, and `auto_write`. This yields 18 distinct runs and records independent API/job assertions in `reports/live_validation/<run-id>.jsonl`, plus frozen scope, job IDs, batch IDs, status, and stage snapshots in `reports/functional/<run-id>/journey-manifest.json`. It waits up to 45 minutes for each run and continues with the next combination after a job reaches terminal state or request failures.

Before running it, verify `/health/ready` and the current PostgreSQL, Ray, Neo4j, and Weaviate endpoints. It performs actual dev store writes under newly onboarded QA customers. It does not delete prior data. After the matrix, prepare HITL concurrency items using the journey manifest; the preparation step requires 140 unique persisted pending review items for the full 20/30/40/50 ramp.

```bash
.venv/bin/python tests/functional/ingestion_golden/run_industry_policy_matrix.py
.venv/bin/python tests/performance/ingestion/prepare_hitl_load_items.py \
  --base-url http://127.0.0.1:6004 \
  --journey-manifest reports/functional/<run-id>/journey-manifest.json \
  --output reports/performance/<run-id>-hitl-items.json --minimum 140
```

The matrix exercises onboarding, contextualization, explicit product-line selection, directory submission, policy selection, job detail polling, the registered 15 stages, and report retrieval. Separate evidence cases cover SSE replay, cancellation/resume, authorization, individual HITL decision variants, PostgreSQL checkpoint/event/store ledgers, and direct graph/vector-store provenance. Controlled failure branches need dedicated checkpoints or review items.

## HITL-intentional synthetic documents

The six generated ten-page documents use domain-specific business content. Each has two distinct `System` inventory entries sharing an intentionally ambiguous alias and one `TechnicalSystem` interface specification with a system-to-system `INTEGRATES_WITH` relation whose target cannot be distinguished from conflicting owner statements. The first live extraction classified the earlier prose as `Narrative` and extracted repeated QA boilerplate as irrelevant graph entities; the fixture now gives the interface one typed challenge and keeps expected behavior in the manifest rather than the source document. The source generator is `generate_synthetic_assets.py`; asset contract checks are in `test_synthetic_hitl_assets.py`.

The live matrix now reads persisted scoped HITL queue rows for both review policies. For `review_ambiguous`, it additionally requires at least one `RELATIONSHIP_REVIEW_REQUIRED` resolution with two or more persisted candidates and writes sample candidate evidence into the journey manifest. A nonempty queue by itself does not satisfy the ambiguity acceptance criterion. The new fixture is a designed challenge, not proof that the LLM extracted it correctly; the live assertion is the proof point. `review_all` must also produce persisted pending items, while `auto_write` must complete without using review as its normal route.

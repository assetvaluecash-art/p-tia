# Ingestion golden run: defect and evidence register

**Run:** `golden-matrix-20261014T020020-2d5b71`  
**Scope:** Live API and persisted-store validation for the banking customer / Consumer Retail / Retail Cards fixture.  
**Evidence root:** [`reports/functional/golden-matrix-20261014T020020-2d5b71/`](../reports/functional/golden-matrix-20261014T020020-2d5b71/)  
**Case ledger:** [`reports/live_validation/golden-matrix-20261014T020020-2d5b71.jsonl`](../reports/live_validation/golden-matrix-20261014T020020-2d5b71.jsonl)

This is a live, partial validation of one industry and one product line. It is not the three-industry golden run or the planned concurrency run. Existing user data was not cleaned or deactivated. Each live iteration used the isolated golden customer and retained its artifacts.

## Current result

The case ledger contains **58 independent assertions: 43 PASS and 15 FAIL (74.14% first-pass)**. One failure is a harness/setup issue, not an application defect: the initial v6 request used the wrong environment token and received HTTP 401; the request was then retried with the documented test actor. The remaining failures are application or coverage defects.

`review_all` iteration 5 completed all 15 stages and produced 44 batch-scoped review rows with zero automatic writes. The `review_ambiguous` iteration 6 completed all stages and produced 15 same-type ambiguous System review items. Both flows exercised audited review decisions. Iteration 7 completed all 15 stages as `COMPLETED_WITH_REVIEW_PENDING`: 41 entities, 13 relationships, and 32 batch-scoped pending review items. All 32 admission IDs reconciled to the batch. It produced 14 same-type entity ambiguity rows but **zero relationship endpoint review rows**. The original runner summary said PASS because it only gated on entity ambiguity; its summary and an appended independent ledger case have now been corrected to FAIL for relationship-review coverage. Follow-up actions exercised one `INTEGRATES_WITH` triplet plan: reuse was approved and audited, but graph execution failed endpoint scope validation. A reject and leave-unresolved action both persisted as expected.

## Defects and coverage gaps

| ID | Severity | Finding | Evidence / touchpoints | State |
|---|---|---|---|---|
| DEF-01 | High | Run-level Neo4j write counts were domain-wide totals, not this run's writes. Reports could claim writes when this run wrote none, and overstate writes when it wrote a subset. | Root cause was both the writer's global/domain totals and a domain-wide verification query overwriting run counters. The working-tree fix scopes read-back by customer, domain, product line, and ingestion batch; if read-back is unavailable it preserves writer counters and marks verification unavailable. The report now distinguishes verified from writer-reported counts and omits global store totals. Unit/regression evidence: TR-035. Earlier live comparisons: 732/95 reported with zero admitted graph blocks; another run reported 779/106 while batch-scoped Cypher returned 45/11. See TR-022 and the `BANK-RET-RA-NEO4J-INGESTION-COUNT-ACCURACY` / `BANK-RET-RA-AMBIGUOUS-NEO4J-COUNT-ACCURACY` rows. | Code fix and focused local regression tests pass; fresh live golden confirmation pending |
| DEF-02 | High | Submission `context_tags` / `project_ids` are not carried through the scheduler-to-worker handoff into stored block provenance. | Submission metadata exists in PostgreSQL, but scheduler worker args and `scripts/run_ingestion_job.py`'s service call omit these values. Weaviate objects in the live run had `context_tags=[]` despite supplied tags. `BANK-RET-RA-CONTEXT-TAGS-TO-STORES`, `HITL-V5-CONTEXT-TAGS-WEAVIATE`. | Code fix in working tree; live confirmation pending |
| DEF-03 | High | Relationship endpoint ambiguity is not reliably converted into a `RELATIONSHIP_REVIEW_REQUIRED` HITL row. | The ten-page fixture has explicit System-to-System interface ambiguity, but iterations 4, 6, and 7 did not establish endpoint-review coverage. Iteration 7 had 14 same-type entity ambiguity rows and zero relationship review rows; its 32/32 batch reconciliation passed. See its `capture-summary.json` and appended policy-gate FAIL row. | Open |
| DEF-04 | High | Approved HITL writes with relationship operations fail endpoint scope validation, including a triplet whose stored resolution names an existing typed System candidate. | Reproduced in TR-030: fresh review `rv-5f4d0bb0848f` has `Digital Banking App -[INTEGRATES_WITH]-> Card Authorization Service`; `APPROVE_AS_NEW` returned HTTP 200/APPROVED but execution FAILED: `Relationship endpoints are missing or outside the frozen customer/domain scope`. Historical examples and retries failed similarly. Fresh evidence: `reports/functional/golden-matrix-20261014T091809-d15beb/hitl-decision-actions.jsonl`; detailed historical plan/action evidence remains in `fixture-iteration-7-review-ambiguous-policy-shaped/review-actions-followup.jsonl`. Touchpoints: `hitl_api.py`, `hitl_write_executor.py`, review repository. | Code fix in working tree; live confirmation pending |
| DEF-05 | Medium | No explicit terminal `SKIPPED` outcome is persisted for Neo4j when a `review_all` run has no eligible graph writes. | Iteration 5 store ledger lacked an explicit Neo4j no-op outcome. This leaves per-store UI/status evidence incomplete. `HITL-V5-NEO4J-STORE-LEDGER`. | Open |
| DEF-06 | Fixed, regression required | Review plan IDs were globally unique but omitted batch identity, allowing a later run to reuse prior review rows and statuses. | Fixed by adding `ingestion_batch_id` to plan identity in `step_build_execution_plans` and passing the batch from `ingestion_service.py`. Regression and fixture checks passed 3/3. Keep the live run-scoped reconciliation assertion. | Fixed in working tree |
| DEF-07 | Medium | Iteration 7 Lane 2 Pass 2 took about 61 minutes before continuing to store writes and reporting. | Job `ingest-consumer_retail-20261014-035254-e453`, run `42617ef9-b818-477b-82c7-7fc8d8c71f6b`; `job-snapshots.jsonl` records roughly 3,686,597 ms for Pass 2. Investigate model call count, retries, and gateway latency against an agreed SLO. | Open performance investigation |
| DEF-08 | Harness | The first v6 submission attempt used an unrelated auth token and received HTTP 401 before job creation. | `HITL-V6-WRONG-ENV-TOKEN-SETUP`; retried successfully with the test actor. | Closed; exclude from product defect rate |

| DEF-09 | High | `review_ambiguous` creates some entity admission reviews but does not reliably persist relationship endpoint review rows; `auto_write` required an explicit same-type resolution rule. | Fresh 2026-10-14 matrix: auto_write completed all 4 Banking/Insurance domain legs with zero pending HITL rows. `review_ambiguous` produced 9, 6, and 1 pending rows on Banking Retail, Banking Lending, and Insurance Health, but no multi-candidate `RELATIONSHIP_REVIEW_REQUIRED`; Insurance Personal Auto produced zero review rows. Evidence: `reports/live_validation/golden-matrix-20261014T091809-d15beb.jsonl` and `reports/functional/golden-matrix-20261014T091809-d15beb/journey-manifest.json`. | Auto-write confirmed for these journeys; relationship ambiguity and P&C review-ambiguous defects remain open. |

### DEF-09 follow-up — policy-gate fix in working tree

`auto_write` now selects the top-ranked candidate only when its node type exactly matches the extracted entity type. It applies the same rule to ambiguous relationship endpoints. The selected candidate, original decision, rank, fusion score, candidate count, and gate reason are carried into the execution plan for audit. If a type-safe candidate is absent, the plan remains in HITL instead of silently dropping or misdirecting a relationship. Targeted tests passed 124/124 and are persisted under `reports/functional/policy-gate-fix-20261014/`.

The fresh matrix seeded source-grounded System nodes from `review_all` approvals before each ambiguity run. Candidate retrieval was observed in Banking Retail logs (two same-type candidates; lexical scores 1.0 and 0.813559), and the review queue persisted entity-level candidate items. However, the matrix found no persisted relationship endpoint review rows in any of the four Banking/Insurance domain legs. Insurance Personal Auto additionally returned zero pending review rows. This is now a confirmed live coverage/policy defect, not only a fixture setup gap.

The same matrix confirmed the in-tree `auto_write` gate behavior across Banking Retail, Banking Lending, Insurance Health, and Insurance Personal Auto: all four completed with zero pending review rows. Admission telemetry for Insurance Health reports 49 entities and 30 relationships routed to write. Store write verification is still limited by the existing domain-wide Neo4j counts and Weaviate before-count warnings; see DEF-01 and the captured job artifacts. Do not infer run-scoped store success from the terminal status alone.

The 2026-10-14 matrix completed with a fresh API/Ray preflight: 110 persisted cases, 105 passed, 5 failed (95.45% first pass). All five failures are endpoint-ambiguity acceptance checks: no multi-candidate relationship-review rows in Banking Retail, Banking Lending, Insurance Health, or Insurance Personal Auto; the P&C leg also failed its pending HITL policy assertion. Full per-case ledger: `reports/live_validation/golden-matrix-20261014T091809-d15beb.jsonl`.

## Review action observations

- `APPROVE_REUSE` against an exact persisted same-type candidate succeeded and did not create a duplicate node.
- `REJECT` persisted reviewer identity/action and did not write a graph node.
- `LEAVE_UNRESOLVED` retained the item as pending.
- A source-grounded `APPROVE_AS_NEW` succeeded for one standalone node candidate.
- `APPROVE_AS_NEW` with a relationship to a not-yet-persisted in-batch placeholder failed endpoint scope validation; retry did not recover it.
- Iteration 7 `APPROVE_REUSE` exercised a stored `INTEGRATES_WITH` triplet and a persisted typed endpoint; the action was approved and audited, but the relationship write failed endpoint validation. This is a triplet write failure, not a successful one.
- No item across iterations 5, 6, or 7 contained `RELATIONSHIP_REVIEW_REQUIRED` with two or more endpoint candidates. Endpoint-choice review actions remain untested and distinct from the failed auto-resolved triplet write.
- The run-level ledger must distinguish HTTP acceptance/audit persistence from successful execution. An HTTP 200 with `execution_status=FAILED` is a failed write action.

## Follow-up sequence

1. Produce a valid relationship-review item, then test endpoint `APPROVE_REUSE` with a persisted ranked candidate, relationship `REJECT`, and `LEAVE_UNRESOLVED`. Existing fixture runs produced no such item, so endpoint-choice actions are not yet validly exercisable.
2. Fix DEF-04 and add an executor regression proving either atomic rejection before marking a decision approved or successful creation/resolution of the in-batch endpoint.
3. Confirm DEF-02's code fix with a fresh run: verify submitted values in PostgreSQL, Weaviate blocks, Neo4j nodes/relationships, and HITL approval writes.
4. Fix DEF-01 and DEF-05 so each store records run-scoped counts and explicit `SKIPPED`/zero-write terminal results.
5. Improve fixture/runtime relationship resolution for DEF-03. Require a persisted row with `RELATIONSHIP_REVIEW_REQUIRED`, multiple typed endpoint candidates, and audited approve/reject outcomes; source text alone is not coverage.
6. Rerun review_all and review_ambiguous on fresh isolated runs after fixes; preserve previous iterations unchanged and append new case rows.
7. Finish remaining banking, insurance, and healthcare domain/product-line × policy combinations, including `auto_write`; verify customer→industry→domain→product-line→function→subfunction graph structure and provenance.
8. Complete API/SSE, report, checkpoint, restart, cancellation, and resume branches, including cancel-versus-publish races and resume after user cancellation, exhausted retry, and infrastructure loss.
9. Reconcile the unfinished/stranded 20-user ingestion rung before higher concurrency. Then run separate 20/30/40/50 ingestion and HITL ramps with p50/p90/p95/p99, terminal counts, queue/capacity evidence, and error distribution.

## Data quality and interpretation

The live fixture is deliberately designed to exercise same-type System ambiguity and a relationship endpoint conflict. The observed pipeline reliably produces entity-level ambiguity but has not yet produced the required relationship-level review case. The current `review_ambiguous` gate correctly fails on missing relationship review coverage. Candidate samples, source excerpts, scores, decisions, and histories should be reviewed from each iteration's JSON artifacts before accepting a fixture revision.



### DEF-04 code follow-up — temporary source endpoint translation

The approved-plan writer now resolves a relationship endpoint through its `source_temporary_id` / `target_temporary_id` mapping before falling back to the snapshot entity ID. This addresses the reproduced plan shape where a newly approved primary node had the same extraction temporary ID in both fields, causing Neo4j to MATCH a nonexistent source node instead of the deterministic customer-scoped ID created earlier in the same transaction. Regression evidence: `reports/functional/def04-hitl-endpoint-fix-20261014-2/case-results.jsonl` (31/31 focused tests pass). The dev API was unreachable during this follow-up, so a fresh persisted HITL item and a live Neo4j relationship write still need confirmation after the service is running the updated code.

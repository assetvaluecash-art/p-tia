# Knowledge Fabric V2.5 adapter

`POST /query/tia` returns a finished TIA Change Impact Report in Knowledge Fabric's agent wire format (`QueryResponse`), so agents that only
speak KF V2 can read software impact. It implements option **B** of `TIA_KF_V2_INTEGRATION.docx` (a sibling route; KF's own `/query` is untouched).
The native report (`GET /api/v1/impact-analyses/{id}`) remains the product deliverable.

## Request

```json
{"intent": "software_tia", "query": "What does PR 1842 affect?", "domain": "corporate_commercial",
 "top_k_semantic": 10, "top_k_graph": 10,
 "analysis_id": "<id>"}
```

Instead of `analysis_id`, give `repository` plus `pull_request` or `revision` (a commit or a 7+ character prefix), or `issue_key` (a key such as
`PAY-4521` that a change named in its commit messages, pull request title or branch; every report records them as `change.issue_keys`): the
newest such report is used, and when several name the key the others are listed in `metadata.errors` as a note.
**The question text is never parsed**: without a selector the answer is an empty response with an explanation in `metadata.errors`. A router in
front of both retrievers finds those tokens in the question and looks them up exactly with `POST /query/tia/resolve` (structured anchors only,
free text refused): see [KF-BRIDGE.md section 13](KF-BRIDGE.md) and the reference router `tia/kfrouter.py`.

`intent` may be `software_tia` or one of Knowledge Fabric's own pattern names for a change, `change_impact`, `regression_scope`,
`test_coverage` or `any`; the first three order the answer for that question. Other intents are Knowledge Fabric's alone and get an empty
answer. Optional: `customer_id` (Knowledge Fabric's customer; any other than the tenant's `knowledge_fabric.customer_id` gets an empty answer),
`domain` (keeps that domain's business chunks only) and `product_line` (a product line's name or id: its services, systems and test cases only). No analysis is
started by this route; queue one with `POST /api/v1/impact-analyses` first. Unknown request fields are refused (422). Needs the `read` permission and
the usual credential; the report is redacted for the caller exactly as in the native API.

## Response mapping

| TIA | QueryResponse |
|---|---|
| each finding | a `ChunkItem` (`block_type: "tia_finding"`, `block_id: "<analysis>#<n>"`, `category`: impact type, `kg_node_type`: node type) and, when it has an impact path, a `PathItem` |
| severity x evidence | `score`: critical 1.0, high 0.85, medium 0.6, low 0.35, halved for `inferred`/`stale` |
| evidence status, rule, strategy, configuration | `extracted_fields` (stringified JSON) and `source_evidence`; text of a weak finding says "treat this as a lead, not a fact" |
| impact path | `path_text` in TIA's stored edge direction, e.g. `(endpoint:"payments-service:GET /payments/{}/status")<-[:CONSUMES_API]-(callsite:"...")`; `relationship_type` is the first hop's real TIA relationship |
| unknowns | `ChunkItem`s with `category: "unknown"`, always appended to `reranked_chunks` whatever `top_k` is; those that affect confidence also appear in `metadata.errors` |
| status `partial` / `complete_with_gaps` / `failed`, redaction, truncation | `metadata.errors` |
| material findings | ranked first in `reranked_chunks` / `reranked_paths` |
| **test scope** (what a tester must test, [USER-GUIDE 4a](USER-GUIDE.md#4a-what-to-test-for-testers)) | one `ChunkItem` per entry point to test (`category: "test_scope"`, `block_type: "tia_test_target"`, text starts `TEST`), per rule gap (`"rule_gap"`, `"tia_rule_gap"`) and per consistency check (`"consistency_check"`, `"tia_consistency_check"`); each entry point's impact path is a `PathItem` (`source_block_id: "<analysis>#test-<n>"`). In `reranked_chunks` they follow the findings, rule gaps first, at most `top_k_semantic` of them; `score` is breaking 0.9, behaviour change 0.6, unknown 0.4, compatible 0.3, halved for weak evidence |
| **tester pack** (the entry points the change reaches: coverage gate, locator pack, API matrix; [USER-GUIDE 4c2](USER-GUIDE.md#4c2-tester-pack-for-one-change)) | `coverage_gate` (one chunk, `block_id: "<analysis>#gate"`, text starts `COVERAGE GATE: COVERED / GAPS / NOT EVALUATED`); `coverage_gap` (one per entry point or page that is not fully covered, text starts `COVERAGE GAP` or `COVERAGE NOT EVALUATED`; the aspects not covered are in `extracted_knowledge`); `api_matrix` (one per touched endpoint: inputs with rules, valid and invalid values, error statuses and up to 60 generated cases in `extracted_knowledge`); `ui_locators` (one per page that calls a touched endpoint: up to 80 elements with CSS / XPath / Playwright locators, mandatory flags and dropdown values in `extracted_knowledge`). `extracted_knowledge` is stringified JSON. In `reranked_chunks` they follow the test scope, in that order, at most `top_k_semantic` of each kind, before the unknowns. "Covered" means a test addresses the aspect, never that it passes. Send `"include_tester_pack": false` to leave them out. The full pack is `GET /api/v1/impact-analyses/{id}/tester-pack` |

## Verification status

Tested against the field lists printed in the integration document (`tests/test_kfadapter.py`), and **checked against Knowledge Fabric 2.5's own `retrieval/retriever_api.py`**: every field and type of `ChunkItem`, `EntityRef`, `PathItem`, `DetectedDomain`, `RetrievalMetadata` and `QueryResponse` is the same, and TIA's `QueryRequest` accepts every field of Knowledge Fabric's (`tests/test_kfbridge.py::test_the_agent_interface_matches_knowledge_fabrics_retriever`, run whenever the source is present in `development/reference/knowledge-fabric-2.5/`). KF's facade or agent wiring is not part of TIA.

When the Knowledge Fabric bridge is enabled ([KF-BRIDGE.md](KF-BRIDGE.md)), the response also carries the business side of the change: one `business_summary` chunk, one `business_test_case` chunk per Knowledge Fabric test case and one `business_system` chunk per affected System, with the chain service → System → requirement → test case as a `PathItem`. For the intents `regression_scope`, `test_coverage` and `change_impact` they are ranked first. When repositories are mapped to products ([KF-BRIDGE.md section 11](KF-BRIDGE.md)), one `business_product` chunk per product line the change reaches (text starts `PRODUCT`), ranked first for `change_impact`.

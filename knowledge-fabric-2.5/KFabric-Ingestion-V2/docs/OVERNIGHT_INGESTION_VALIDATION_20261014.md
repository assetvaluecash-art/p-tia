# Overnight ingestion validation status — 2026-10-14

## Agreed graph structure

`CustomerRoot → DomainRoot → ProductLine → Function → SubFunction` is the graph hierarchy. Industry is customer metadata (`industry_id`), never a root. New Weaviate blocks carry `customer_root_id` as a stable customer scope key; the class schema version is 1.2. Scoped Neo4j migration moves domain roots off an obsolete per-customer `IndustryRoot` and removes that obsolete node.

## Work prepared

- Updated Neo4j ingestion and HITL writes to create `CustomerRoot` and attach domain roots to it.
- Updated Weaviate schema and block metadata with `customer_root_id`.
- Strengthened health insurance extraction examples for `Control`, `AcceptanceCriteria`, and `Incident`, alongside the existing care, pharmacy, banking, and insurance policy work.
- Prepared the 21-journey matrix: seven domain/product-line fixtures across three industries, each exercised with `review_all`, `review_ambiguous`, and `auto_write`.
- Added the real corporate banking sample `SMB_Process_Flow.docx` and wealth sample `Equity_Trading.docx` as companions to generated ten-page domain fixtures.
- Added HTML output for completed matrices and an honest HTML preflight blocker report.

## Validation

- `tests/ingest/test_hitl_write_executor.py` and `tests/ingest/test_industry_aware_store_routing.py`: **10 passed**.
- YAML parsing, Python compile checks, and `git diff --check`: **passed**.
- The functional matrix was invoked twice, including once with a read-only service preflight. Both the ingestion API `http://192.168.68.17:6002/health` and Ray Jobs API `http://192.168.68.17:8266/api/version` closed connections before returning a response. Weaviate readiness (`:8080`) and Ollama tags (`:11434`) returned HTTP 200.
- Functional run `golden-matrix-20261014T181428-8781bc` stopped before onboarding or ingestion: **0 customers created, 0 jobs submitted, no store cleanup**. This is an infrastructure preflight result, not an ingestion pass/fail result.
- Performance ramps were held because their required functional batch/review scopes do not exist yet. No load jobs or HITL decisions were sent.

## Reports

- [Functional preflight HTML](../reports/functional/golden-matrix-20261014T181428-8781bc/PREVIEW.html)
- [Functional preflight JSON](../reports/functional/golden-matrix-20261014T181428-8781bc/preflight-blocker.json)
- [Performance hold HTML](../reports/performance/20261014/PREVIEW.html)
- [Performance hold JSON](../reports/performance/20261014/summary.json)

## Resume sequence

1. Restore the ingestion API and Ray Jobs API, then confirm `/health` and `/api/version` answer successfully.
2. Run the functional matrix:

   ```bash
   cd KFabric-Ingestion-V2
   .venv/bin/python tests/functional/ingestion_golden/run_industry_policy_matrix.py \
     --base-url http://192.168.68.17:6002 \
     --ray-url http://192.168.68.17:8266 \
     --wait-minutes 45
   ```

3. Review its generated `PREVIEW.html`, `summary.json`, and `journey-manifest.json`. Only continue to load once functional ingestion is clean and its manifest contains completed jobs with persisted review scopes.
4. Prepare at least 140 unique pending reviews for the HITL load ramp:

   ```bash
   .venv/bin/python tests/performance/ingestion/prepare_hitl_load_items.py \
     --base-url http://192.168.68.17:6002 \
     --journey-manifest reports/functional/<successful-run-id>/journey-manifest.json \
     --output reports/performance/<run-id>/hitl-items.json
   ```

5. Run the agreed 20/30/40/50 concurrency rungs for ingestion and HITL as documented in [the performance README](../tests/performance/ingestion/README.md). Capture the generated JSON and HTML before drawing conclusions.

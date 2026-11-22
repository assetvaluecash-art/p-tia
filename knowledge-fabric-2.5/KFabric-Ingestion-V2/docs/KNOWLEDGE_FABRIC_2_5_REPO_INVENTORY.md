# Knowledge Fabric 2.5 repository inventory

**Status:** First-pass, non-destructive packaging boundary. No source, data, or runtime files were moved or deleted while preparing this inventory.

The target is a separate ADO repository built from `KFabric-Ingestion-V2`. Keep the currently deployed repository and service untouched. This inventory distinguishes the likely package from local runtime content and areas that require a usage or ownership check before exclusion.

## Candidate package contents

| Path / group | Initial decision | Reason / next check |
|---|---|---|
| `kbfabric_ingest/` | Include | Application source for the API, scheduler, ingestion, customer/ontology services, HITL, security, retrieval, and reporting. Confirm installed entry points and imports from a clean checkout. |
| `config/` | Include | Ontologies, extraction policies, retrieval patterns, and application configuration. Review for embedded credentials, local paths, and test-only data. |
| `tests/` | Include selectively | Keep current unit, contract, security, functional, and performance tests with their fixtures. Inspect old top-level tests and external-service requirements before calling the suite runnable. |
| `scripts/` | Include selectively | Keep reproducible setup, migration, deployment, evaluation, and validation scripts. Inspect each script's paths, destructive operations, credentials, and relationship to current workflows. |
| `docs/` | Include curated docs | Keep current setup, API/UI, operations, dependency, build history, defect, and test evidence notes. Archive or remove contradictory legacy instructions only after checking they are superseded. |
| `pyproject.toml`, `requirements.txt`, Ray requirement files, `pytest.ini` | Include after reconciliation | These define install and test behavior. Compare dependency files, lock files, Python/Ray/NumPy constraints, and container build inputs; document one supported setup path. |
| `Dockerfile.ray-ingest`, `docker/`, startup scripts | Include after validation | These define the runtime and deployment shape. Verify the API port, Ray URLs, worker paths, mounts, health checks, and safe environment injection. |
| `custom_ml/` | Include only if production imports require it | Contains bundled embedding/reranking code and potentially large model assets. Trace imports and model loading; document model license, size, and download/setup method. |
| `tests/functional/ingestion_golden/assets/`, `tests/ingest/golden_pack/`, `tests/ground_truth/` | Include curated synthetic fixtures | Preserve fixtures required by the agreed suite. Do not include source documents or `vision_test_oracles/` in the ADO staging pack, per owner direction. |
| `reports/live_validation/`, selected `reports/functional/`, selected `reports/performance/` | Include selected evidence | Preserve the summary, route matrix, representative HTML reports, JMeter plan, and concise result summaries. Exclude duplicate runs, raw sensitive payloads, vectors, and bulky generated HTML assets unless needed to reproduce a result. |
| `docs/openapi/` and UI handbook | Include after live reconciliation | Compare the maintained contract and handbook to current routes and `/openapi.json`; state which server port exposes interactive docs. |

## Exclude from the new Git repository

These are generated or machine-local runtime state and should be recreated locally or in deployment. Keep them in the separate backup, not in Git:

- `.venv/`, Python caches, `.pytest_cache/`, `.ruff_cache/`, `*.egg-info/`.
- `pip_deps/`, build caches, downloaded package wheels, and dependency/model caches.
- `artifacts/`, `checkpoints/`, `progress/`, `uploads/`, `logs/`, and generated runtime state. Retain selected sanitized reports under `reports/` as described above.
- `nltk_data/` and downloaded model data. Document the exact required NLTK resources and model setup in the install handbook; do not infer from the full local cache.
- Local `env`, `env.test`, `.env*`, tokens, passwords, private keys, and machine-specific endpoint values. Provide sanitized templates with safe placeholders.
- `.e2e_loc_input/`, `temp/`, local `c__/` data, generated `merged`, transient root logs, and `current_job_progress.json`.

The existing `.gitignore` and `.dockerignore` are not a package manifest. In particular, the Docker ignore list has broad data exclusions but does not replace a secret scan and an explicit Git-content review.

## Must investigate before classifying

### Findings from the first reference audit

- `retrieval/` is active application functionality in `kbfabric_ingest/`, with package imports and tests. Keep it in the 2.5 candidate; it is not stale code.
- `drop_zone/` is referenced by operator scripts, existing setup examples, and `tests/test_upsert_quick.py`. Preserve the ability to run those workflows. For the new repo, prefer moving only approved, representative inputs into named fixtures and updating examples to point there.
- No source, test, or current documentation references to `drop_zone - Copy/` were found. This makes it a candidate for exclusion from the staged repo, but its contents still need an ownership/privacy check before sharing or deleting.
- `golden_ingestion_files_1.0/` is referenced by validators and investigation scripts; `tests/test_xlsx_multisheet.py` also reads its `smoke/` directory. Keep the exact test-required fixture until the test is migrated. `scripts/create_smoke_test.py` writes into that directory, so the scripts should be reviewed before packaging.
- `vision_test_oracles/` is referenced by the standalone comprehensive validator and older vision experiments, but is excluded from the ADO staging pack per owner direction. The standalone validator must be excluded too or changed to use approved synthetic fixtures before it is represented as runnable.
- `lift_and_shift_test_design/` has its own legacy vision test and documentation, but no imports from the application or current test suite were found. It is a candidate for exclusion from the runnable app pack; retain only if the team still uses that standalone experiment.
- `=0.6.13` contains a short pip error string and `=3.1.0,` is empty; both are tracked in Git, with the only commit reference found being `683a04dc` (`added dockerfile`). `EnrichedBlock.` and `merged` are empty and untracked. None has a source reference found so far. The two tracked files are strong candidates to omit from the explicit staging allowlist, with their removal from the source repo deferred to a reviewed cleanup change.
- `smoke/smoke/smoke_test_corporate_commercial.docx` is byte-identical to the file under `golden_ingestion_files_1.0/smoke/`. That duplicate can be represented once if any retained workflow needs it.

### Findings from the second screenshot audit

| Items | Finding | Staging recommendation |
|---|---|---|
| `compare_logs.py`, `comprehensive_analysis.py`, `detailed_analysis.py` | Standalone analyses tied to dated 2026-02 job IDs and local log paths; no incoming references found. | Exclude. Preserve selected conclusions in the curated validation record if still useful. |
| `comprehensive_enterprise_validation.py`, `comprehensive_validation.py`, `comprehensive_retriever_tests.py` | Manual validation scripts aimed at localhost services and historical ports/schema; no incoming references found. The maintained test suite is under `tests/`. | Exclude from the runnable pack unless rewritten for current endpoints/config and adopted under `tests/`. |
| `count_pdf_images.py`, `debug_class_name.py`, `debug_retriever_api.py`, `diagnose_weaviate_embeddings.py` | Manual probes with no incoming references. Several point into a sibling `kbfabric-retrieval` checkout or hardcode localhost. | Exclude; recreate a specific probe under `scripts/` only if there is an active operational need. |
| `current_job_progress.json` | Runtime progress snapshot; the scheduler writes this path. | Exclude the checked-in/generated snapshot; retain the application code that creates it. |
| `deploy_to_server.sh`, `Dockerfile.ray-ingest` | Deployment docs call the script and image build. The deploy script sources the parent `env`, copies the working tree with exclusions, and restarts the remote API. | Keep as candidate infrastructure, but review it for 2.5 branch/repo paths, secret-safe operation, and explicit target configuration before use. Do not execute as part of cleanup. |
| `e2e_ingestion_test.log`, `e2e_validation.log` | Historical UTF-16 logs, with no current references found. | Exclude; retain concise, sanitized test summaries instead. |
| `EMF_CONVERSION_SETUP.md`, `extracted_lc_image_*.emf/png`, `extract_images.py` | Belong to the legacy vision/EMF experiment and depend on its sample documents/oracles, which are excluded from ADO staging. | Exclude as one legacy experiment set. |
| `fabric_foundry_openapi_extract.json` | Generated-looking OpenAPI extract; no current references found. The maintained contract is in `docs/openapi/`. | Exclude; use the maintained OpenAPI file and regenerate extracts when needed. |
| `COMPREHENSIVE_FIX_PLAN.md`, `COMPREHENSIVE_WORK_SUMMARY.md`, `FINAL_ENTERPRISE_ASSESSMENT.md`, `FINAL_IMPLEMENTATION_SUMMARY.md`, `FINAL_RESULTS_AND_ASSESSMENT.md`, `FINAL_VALIDATION_PLAN.md` | Historical build/validation notes with overlapping claims and old implementation context; no current code references found. | Exclude from the 2.5 documentation set; preserve the already-curated build history, defects/pending list, and current validation reports. |
| `env`, `EnrichedBlock.`, `extracted_lc_image_*`, job logs | Local configuration, empty accidental file, excluded vision sample assets, or generated output. | Exclude from staging. Keep current local config only in the recovery backup; never add it to the ADO pack. |

### Findings from the third screenshot audit

| Items | Finding | Staging recommendation |
|---|---|---|
| `pytest.ini` | Pytest configuration; project settings are also in `pyproject.toml`. | Keep only one authoritative pytest configuration after checking for overlap; do not drop test configuration from the pack. |
| `final_validation.py`, `wait_and_analyze.py`, `quick_status.py` | They call one another, but use fixed February 2026 job IDs and localhost API assumptions. | Exclude as a runnable suite; replace with current parameterized API checks if the same flow is still needed. |
| `monitor_job.py`, `monitor_job_comprehensive.sh` | `monitor_job.py` is referenced by old wealth submission helpers. Both are manual monitors tied to localhost ports and old API response shapes. | Exclude from the supported pack unless updated to the current job API and documented as an operator tool. |
| `get_job_details.py`, `get_job_error.py`, `full_analysis_report.py`, `list_artifacts.py` | Standalone job/log/artifact inspection scripts with dated defaults or local paths; no incoming references found. | Exclude; preserve selected run findings in the current validation reports. |
| `live_store_validation.py`, `neo4j_quick_validation.py`, `quick_weaviate_check.py` | Manual store checks against localhost and legacy wealth schema/domain assumptions; no incoming references found. | Exclude unless rewritten to use configured endpoints and the current customer/domain scope. |
| `fix_ray.sh` | Legacy recovery script that force-stops Ray and kills matching worker processes. It is documented by the old Ray guide, not by current runtime startup. | Exclude from the runnable pack. Do not run during cleanup. If a supported Ray recovery procedure is needed, replace it with a narrowly scoped, reviewed operator guide. |
| `full_ingestion_test.log`, `ingestion_full.log`, `ingest_api.log`, `jmeter.log` | Local/historical logs or raw JMeter output; no evidence they are required at runtime. The API creates `ingest_api.log` when started. | Exclude log files; preserve a sanitized summary and a maintained JMeter plan/result if they are part of the agreed validation evidence. |
| `job_payload.json` | Saved request payload, not application source; contents may include machine paths or customer data. | Exclude. Keep a sanitized request example in OpenAPI/docs instead. |
| `INGESTION_VALIDATION_REPORT.md`, `KV_CACHE_IMPLEMENTATION_SUMMARY.md`, `PRE_TEST_CHECKLIST.md`, `PROCESS_DIAGRAM_EXTRACTION_FIX_CHANGELOG.md`, `RAY_COMPLETE_FIX.md` | Historical reports or implementation notes. Some refer to excluded vision data, old architecture, or old local Ray setup. | Exclude from the published docs set unless an item is reconciled into the current build history, defect list, or operations handbook. |

### Findings from the fourth screenshot audit

| Items | Finding | Staging recommendation |
|---|---|---|
| `requirements.txt`, `ray_job_requirements.txt` | Separate service and Ray worker dependency sets. The Dockerfile and startup scripts consume the service requirements; the Ray dependency download/compile scripts consume the Ray set. | Keep both, then reconcile Python/Ray/NumPy and package versions for the supported image. |
| `ray_job_requirements.lock` | Generated pinned lock file; `scripts/compile_ray_requirements.sh` creates it and `scripts/download_pip_deps.sh` prefers it. | Keep and regenerate from the reviewed Ray requirements after dependency versions are agreed. |
| `ray_job_requirements_offline.txt` | Uses `/app/kbfabric-ingest/pip_deps`, has broad unpinned ranges, and has no current script or docs references found. | Exclude unless offline worker installation is intentionally supported; if retained, make it consistent with the lock and documented image path. |
| `start_ingest_api.sh`, `start_ingest_api_dev.sh`, `start_ray_cluster.sh`, `start_scheduler_ui.sh` | Current README/operations docs call these startup entry points; they define API, Ray, and UI launch behavior. | Keep as runtime infrastructure and validate their variables, port assumptions, and safe process handling for 2.5. |
| `start_scheduler_api.ps1` | Hardcodes one Windows checkout path and port 8001; no current references found. | Exclude or parameterize and document if Windows local startup remains supported. |
| `submit_consumer_retail.py`, `submit_final_wealth.py`, `submit_ingestion_final.py`, `submit_job_final.sh`, `submit_job_fixed.py` | Manual submitters with old WSL paths, localhost ports, and `force_rewrite: true`; no current references found. | Exclude from the supported pack. Use current API examples and tests with explicit isolated test data instead. |
| `retriever_api_tests.py`, `retrieval_bm25_probe.json`, `retrieval_test.json`, `retrieval_test.log` | Standalone old retriever port 6004 test and saved outputs; no current suite references found. Retrieval app code remains active, but these probes are not current contract tests. | Exclude these old probes/artifacts; retain active retrieval package code and maintained tests. |
| `replace_code.py` | One-off script that edits `kbfabric_ingest/llm/client.py` by regex; no references found. | Exclude. Do not run as a migration or cleanup step. |
| `README.md` | Authoritative root README consumed by packaging metadata, but some run instructions still point at `no_tmux_wsl_scripts/`. | Keep and reconcile paths with the actual startup scripts before publishing. |
| `README.docx`, `README.html`, `READY_TO_TEST*.md`, `START_INGESTION.md`, `REMAINING_FIXES_BATCH.md`, `stores_report.md`, `RAY_COMPLETE_FIX.md` | Historical or alternate setup/report documents; content overlaps and includes old architecture/vision or local machine assumptions. | Exclude from the published docs set; keep current README, setup handbook, build history, defect list, and validation evidence. |
| `report.html`, `report.json`, `scheduler_api.log`, `retrieval_test.log`, and other root logs/outputs | Generated local report, request/result artifacts, or logs; may include operational data. | Exclude. Keep only selected sanitized reports under the curated reports directory. |
| `submit_job_fixed.bv` shown in the screenshot | That exact filename is not present; repository contains `submit_job_fixed.py`, which is a legacy submitter. | Exclude the `.py` submitter as above. |

### Findings from the fifth screenshot audit

| Files | Finding | Staging recommendation |
|---|---|---|
| `submit_job_fixed.py`, `submit_wealth_final.py`, `submit_wealth_mgmt.py`, `submit.py` | One is syntactically invalid; the others submit old wealth-management jobs to localhost ports using a hardcoded WSL drop-zone path and `force_rewrite: true`. No current code or test references. | Exclude all four legacy submitters. |
| `SYSTEM_DEPENDENCIES.md` | Referenced by a comment in `requirements.txt`, but its content is an older Vision IE / EMF conversion setup note. | Reconcile required system packages with the current deployment handbook; do not publish this stale note as authoritative. |
| `TABLE_EXTRACTION_FIX_CHANGELOG.md`, `VISION_IE_FIX_SUMMARY.md` | Historical implementation notes from 2026 experiments; no runtime consumers. The latter is linked from another historical completion note. | Exclude from published docs; preserve only relevant current behavior/defect notes in maintained docs. |
| `test_api_with_logging.py`, `test_retriever_fixed.py`, `test_retriever_queries.py` | Standalone retrieval API probes targeting old `localhost:6004/query`; unrelated to the ingestion service API validation suite. | Exclude from the supported test pack. |
| `test_arctic_dim.py`, `test_retriever_embeddings.py` | Manual model-loading diagnostics. One loads the old retrieval project/config path; neither is referenced by maintained tests. | Exclude unless a retrieval-specific maintained test is intentionally added. |
| `test_ingestion_fixed.py`, `test_ray_import.py`, `test_ray_with_env.sh`, `test_simple_job.sh` | Legacy standalone probes; include hardcoded local services or old Ray CLI assumptions, and the ingestion probe submits with `force_rewrite`. No maintained-suite references. | Exclude; validate these behaviors through supported API/functional checks instead. |
| `test_schema_exists.py`, `test_weaviate_direct.py`, `test_weaviate_schema.py`, `test_weaviate_vector.py`, `test_specific_image_ie.py` | Ad hoc local-store diagnostics with hardcoded localhost endpoints; some assume old Weaviate class names/data. The image test expects a historical wealth diagram. | Exclude from the runnable pack; retain evidence only if useful and sanitized. |
| `temp_debug_tolerance.py` | Diagnostic script imports the old retrieval package via a hardcoded `/mnt/c/...` path. | Exclude. |
| `test_specific_image_ie.py`, `test_vision_api_generate.py`, `test_vision_ie_standalone.py`, `test_emf_fix.sh`, `TEST_VISION_FIX.sh`, `TEST_VISION_FIX_V2.sh`, `test_vision_oracles.py` | Vision/EMF experiments depend on historical source documents, images, or `vision_test_oracles/`, which the owner has excluded from ADO staging. | Exclude together with those sample/oracle assets. |
| `validate_ingestion.py` | Historical store validator contains hardcoded localhost endpoints, a literal Neo4j password, and stale expected counts/domain. | Exclude; do not run against shared development stores. |
| `validate_stored_knowledge.py` | Temporary store inspection script; uses current app config but inspects broad store contents and has no maintained-suite reference. | Exclude from supported pack unless rewritten as a scoped, safe validation tool. |

### Findings from the sixth screenshot audit

| Files / directories | Finding | Staging recommendation |
|---|---|---|
| `.e2e_loc_input/` | Contains a real Letter of Credit sample document used as a local E2E input; it is not a synthetic fixture. | Exclude from ADO staging under the source-document exclusion. |
| `.pytest_cache/`, `.ruff_cache/`, `.session/` | Generated test/lint cache and local session log. | Exclude as machine/run output. |
| `.venv/` | Local virtual environment; already ignored by Git. | Exclude; recreate from the declared dependency files. |
| `artifacts/` | Per-job generated artifacts and diagnostics, with dated run directories. | Exclude wholesale from the runnable pack; selectively retain reviewed test evidence in the reports area if needed. |
| `c__/` | Appears as an empty stray directory in the checkout; no references found. | Exclude. |
| `vision_test_oracles/` | Already covered above; it supports legacy vision checks and was explicitly excluded from staging. | Exclude with its dependent legacy vision scripts. |
| `_ie_gap_analysis.py`, `_mega_verify_final.py`, `_mega_verify.py`, `_reseed_spine.py`, `_seed_pool_actual.py`, `_verify_post_ingest.py` | Standalone analysis/verification or store-seeding scripts, already assessed in the earlier screenshot audit. | Exclude from the supported pack; preserve in the source backup. |
| `VISION_IE_FIX_SUMMARY.md`, `wait_and_analyze.py` | Already covered in earlier audit: historical vision note and fixed-ID waiter that calls legacy status/validation scripts. | Exclude from the published docs and runnable pack. |
| `wait_for_completion.py` | Monitors a fixed February 2026 job ID and log path, sleeps for up to 20 minutes, then calls legacy `localhost:9000`; no maintained-suite reference. | Exclude. |

| Path / group | Why it is unresolved | Investigation |
|---|---|---|
| `drop_zone/` and `drop_zone - Copy/` source documents | Reference audit is complete, but source ownership/privacy is unknown. | Before adding any of these documents to ADO, confirm they are approved for repository sharing. If not, exclude them and keep synthetic fixtures. |
| `smoke/`, `golden_ingestion_files_1.0/`, and vision oracles | Current test/script references include legacy and non-synthetic inputs. Owner directed that source documents and vision oracles be dropped from ADO staging. | Exclude source inputs and vision oracles. Keep only approved synthetic assets; update or omit consumers that require excluded files. |
| `lift_and_shift_test_design/` | Standalone legacy experiment with no current application/test imports found. | Exclude from the initial runnable pack unless the team confirms it is still used. |
| Suspicious empty/error-string root files | `=0.6.13` and `=3.1.0,` are tracked; `EnrichedBlock.` and `merged` are untracked. No source references were found. | Exclude from the explicit staging allowlist. Defer deleting the tracked pair from the source repo until cleanup review. |
| Retrieval implementation and retriever scripts/tests | Confirmed active functionality; imports/tests and API entry point exist. | Keep it. Review setup and test dependencies as part of clean-checkout validation. |
| Screenshot-listed root scripts | None is invoked by the application, imported by the package, or referenced by current non-Python setup/docs; they are standalone/manual scripts. Several target localhost services, use embedded credentials, point at dated/machine-specific artifacts, or mutate stores. | Exclude from the runnable 2.5 pack unless a named check is deliberately migrated, made safe/configurable, and added to the maintained test suite. |
| Root-level legacy test and analysis scripts | The checked screenshot-listed scripts have no incoming code or documentation references. They are manual experiments, not app dependencies; several contain old local endpoints, embedded credentials, or destructive writes. | Exclude from the runnable staging pack. Preserve source in the backup; decide separately whether any specific probe should be rewritten as a supported test. |
| `reports/` and `logs/` | Large, mixed-age evidence may contain customer-derived content and redundant outputs. | Keep selected evidence with run ID, date, environment, and purpose; review contents for secrets and personal/customer data before publishing. |
| `README.docx`, `README.html`, older root Markdown summaries | May conflict with current setup and endpoints. | Compare with the current README, deployment scripts, and live API; retain only current authoritative instructions. |

## Safe packaging sequence

## Final reconciliation for the first staging pass

This reconciles the repeated screenshot audits into one package boundary. It is a staging decision, not a deletion list. The V2 working tree and its current deployment stay intact; packaging will happen in a separate directory and target a new ADO repository with a distinct name.

### Keep in the staging candidate

- Application source: `kbfabric_ingest/`, `config/`, active `retrieval/` code and `custom_ml/` only where imports confirm it is required.
- Maintained tests under `tests/`, including only approved synthetic fixtures. Keep service-dependent and live tests clearly marked and opt-in.
- Reviewed dependency/build files and current Docker, startup, and deployment assets after reconciling Python/Ray/NumPy versions and removing machine-specific paths.
- Current README, curated setup/API/UI/operations docs, OpenAPI contract, defect/build-history notes, and the maintained API/functional/performance evidence selected for publication.
- Git/build metadata needed for the new repository, with `.gitignore`/`.dockerignore` checked against the staged file list.

### Exclude from the staging candidate

- Local config and secrets: `env`, `env.test`, `.env*`, credentials, tokens, private keys, and machine-specific values. Supply sanitized templates only after variable names and safe placeholders are checked.
- Real or customer-derived source documents, including `drop_zone/`, `drop_zone - Copy/`, `.e2e_loc_input/`, `smoke/`, `golden_ingestion_files_1.0/`, and `vision_test_oracles/`. Keep only explicitly approved synthetic fixtures.
- Local/runtime state and generated output: `.venv/`, caches, `.session/`, `artifacts/`, `checkpoints/`, `progress/`, uploads, raw logs, and unreviewed reports. Select evidence separately after privacy and secret review.
- Root-level legacy manual scripts and old probes identified in the screenshot audits, including fixed-ID submit/wait scripts, unsafe store seeders, old localhost retrieval probes, and legacy Vision/EMF experiments. Preserve them in the recovery backup, not the supported 2.5 pack.
- Duplicate, historical, contradictory, or generated docs/reports and accidental files such as `=0.6.13`, `=3.1.0,`, `EnrichedBlock.`, and `merged`.

### Resolve before staging or publishing

| Item | Required decision/check |
|---|---|
| Source fixtures | Exclude all non-synthetic inputs. Confirm the retained synthetic fixture set contains no copied source text or identifying data. |
| Dependencies and runtime | Reconcile `pyproject.toml`, service and Ray requirements/lock files, Dockerfile, startup scripts, model/NLTK resources, and OS packages into one documented clean-checkout path. |
| Deployment scripts | Confirm exact target settings are explicit and safe; remove parent-repo/env assumptions. Do not run deployment scripts during packaging. |
| API contract and docs | Reconcile OpenAPI and UI handbook against implemented routes; identify the API docs URL/port for deployment. |
| Evidence | Select a small set of sanitized run reports and JMeter summary/plan; omit raw logs and duplicate artifacts. |
| Git hygiene | Build the staged file list, scan it for secrets and large/generated files, and inspect the staged diff before creating the new repo. |
| New repository identity | Choose a distinct ADO repo name and confirm project/visibility before repository creation or push. |

The screenshot classifications are recommendations for the staged candidate only. They do not authorize deleting anything from the V2 source tree. An initial, separate copy now exists at `Knowledge-Fabric-2.5-Ingestion-Staging/`; no repository has been created and nothing has been pushed.

### Staged handoff status (15 October 2026)

- The staged candidate is a separate repository tree with `KFabric-Ingestion-V2/` nested below its root so the runtime's repository-root environment lookup resolves to the checkout root. It contains the curated application source, selected tests/synthetic fixtures, configuration, API/UI documentation, and handover notes. The root README and `docs/ADO_REPOSITORY_DETAILS.md` describe the handoff and known limitations.
- The embedding and reranker weight files are omitted from this staged copy at the owner's direction. Their source clients and empty model-directory placeholders remain; the developer will provide the licensed model directories manually. The original V2 model files remain in the V2 working tree.
- Root `env.example` and `env.test.example` list the application environment variable names with blank values. They are templates only and must be populated locally; no actual `env` or `env.test` credentials are included.
- Raw live/functional/performance reports are not included. Some historical notes refer to reports retained in the source working tree; re-run the checks and attach reviewed, sanitized evidence before treating the candidate as release evidence.
- The root README and nested application README have been refreshed for the handoff. Startup scripts and target deployment settings still need review by the developer before execution, especially the Ray helpers that can stop existing local processes.
- Gitleaks reported one generic-key candidate on a deliberately fake test fixture value (`idempotency-key-001`). This exact finding is narrowly listed in the root `.gitleaksignore`; a fresh scan of the staged source reported no leaks. Review this exception if the fixture changes.
- The initial staged copy was committed locally, then the root README, nested README, ADO repository details and this inventory status were updated. The final handoff commit ID is recorded by Git; no remote has been configured yet.
- This is a review candidate, not a runnable or publishable release. No tests were run during this copy step.

## Safe packaging sequence

1. Save this inventory and the current source backup as the recovery point.
2. Resolve the investigation rows using repository-wide reference searches, Git history, and owners for customer data.
3. Build a separate staging directory from an explicit allowlist; do not prune the working source tree to create it.
4. Run secret scanning and inspect staged filenames, file sizes, Git diff, and ignored-file behavior.
5. From the staged copy, create a clean virtual environment, install documented dependencies, start against isolated local PostgreSQL/Ray/Weaviate/Neo4j, and run agreed test groups.
6. Publish the staged candidate to the new ADO repo only after the contents and validation record are reviewable. The current deployment remains unchanged.

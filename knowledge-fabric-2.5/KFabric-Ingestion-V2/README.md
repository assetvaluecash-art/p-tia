# Ingestion application

Application source and runtime assets for the Knowledge Fabric 2.5 ingestion handoff. For project scope, current validation status, defects, and the external-service map, start at the repository root [`README.md`](../README.md) and [`ADO_REPOSITORY_DETAILS.md`](docs/ADO_REPOSITORY_DETAILS.md).

## Runtime requirements

- Python and packages in `requirements.txt` / `pyproject.toml`.
- A Ray cluster reachable by the API process and Ray job runtime.
- PostgreSQL for durable customer, job, review and lifecycle state.
- Weaviate and Neo4j for vector and graph publication.
- Ollama or the configured LiteLLM router for model requests.
- Embedding and reranker weights at the configured local paths when those features are enabled. The model files are intentionally not in this repository; the developer supplies them under the upstream license terms.

Use dedicated local/test services and scopes. Do not run against shared or production stores without an approved test plan.

## Configure and run

The environment loader resolves the checkout root (the parent of this application directory). Copy the root `env.example` to `env`, populate every required setting, and keep that file untracked. For the test profile, copy `env.test.example` to `env.test` and set `KBF_ENV=test`. The templates contain blank values; they are not runnable as-is.

Create a virtual environment in this directory and install `requirements.txt`. Review `ray_job_requirements.txt`, `ray_job_requirements.lock`, and `Dockerfile.ray-ingest` when preparing the separate Ray worker environment. Confirm Python, Ray, NumPy, model, database and Weaviate versions against the target deployment before installing.

Start the external dependencies first, then run the API using the configured host and port. The startup helper `start_ingest_api.sh` reads the root `env` and starts `kbfabric_ingest.scheduler.api:app`; inspect the script and target configuration before use. Set `PYTHONPATH` to this application directory for manual launch. API docs are available at `/docs`, and the live OpenAPI document is `/openapi.json` on the configured API endpoint.

The Ray helper scripts contain host-specific behavior and may stop existing Ray processes. Review them before use; do not execute them on shared hosts as a convenience startup command.

## Tests and validation

Tests live under `tests/`. Select by scope and read each module's markers/configuration before running: some checks require live PostgreSQL, Ray, Weaviate, Neo4j, model services, or write access. Functional and JMeter checks can create records and load on configured services. Use isolated data and explicit target URLs.

The documented 15 October 2026 validation was partial. The root README and `docs/KNOWLEDGE_FABRIC_2_5_DEFECTS_AND_PENDING.md` describe known failures and evidence limitations. No claim is made that the full suite or clean-checkout ingestion has passed.

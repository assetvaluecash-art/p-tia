# Ingestion performance validation

Load runs are separate from functional journey runs and persist one JSONL result row per request plus a run summary. Never treat a request accepted by the API as a completed ingestion. Preserve the run folder and all generated input documents as evidence.

The required ladder is 20, 30, 40, and 50 concurrent virtual users. Run ingestion and HITL in separate invocations using `concurrent_user_ramp.py`. Each rung reports request latency p50/p90/p95/p99, throughput, status-code distribution, and (for ingestion) end-to-end terminal latency and job-state distribution. Do not advance a rung while jobs from the previous rung remain active.

Use development/staging services only. Set `Authorization` to a platform-approved test token, or use distinct `test-<actor>` tokens only where the deployed development auth explicitly supports them. Do not claim Kong-JWT or production-identity coverage for dev test tokens. Use a QA customer and a dedicated product line. Capture the environment/service version, route, run ID, and sanitized response evidence. Do not include bearer tokens or source document text in result files.

Commands are documented in `docs/INGESTION_IMPLEMENTATION_ROADMAP.md` and the test register. The runner refuses a load size above 50 and requires explicit scope/input arguments. It never cancels jobs, clears data, or deletes test customers.

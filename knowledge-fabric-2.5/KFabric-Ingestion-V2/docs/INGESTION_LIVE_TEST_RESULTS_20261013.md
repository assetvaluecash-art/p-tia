# Live Functional Test Results — 2026-10-13

This is the case-by-case outcome register from the first complete live API journey and read-only store verification. The suite uses uniquely named test customers and batch-scoped reads; it did not clear stores or deactivate customers. The current code was exercised against local Ray and development PostgreSQL, Neo4j, and Weaviate.

## Run summary

| Batch | Cases | Passed | Failed | First-pass rate |
|---|---:|---:|---:|---:|
| Live API/context/job/ingestion `live-20261013-fifthpass` | 144 | 136 | 8 | 94.44% |
| Read-only direct store verification | 24 | 16 | 8 | 66.67% |
| **Raw first run** | **168** | **152** | **16** | **90.48%** |
| **Adjusted for two invalid report expectations** | **168** | **154** | **14** | **91.67%** |

The API suite’s eight first-pass failures include six contract mismatches and two report assertions that expected 404 for failed jobs; the service correctly generated reports and returned 200. The adjusted row corrects only those two assertions. Store-verification failures are actual unmet acceptance criteria. These are first-pass outcomes; the test runner was changed to expect HTTP 200 for reports of any terminal job, but the full live batch has not yet been rerun after that expectation correction.

## Safe repository regression

| Suite | Collected outcomes | Duration | Notes |
|---|---:|---:|---|
| Broad repository regression | 545 passed, 25 failed, 3 skipped, 4 errors | 496.58 seconds | Command and excluded unsafe/stale tests are recorded in TR-014 in the functional test register. All failures were allowed to complete and remain visible for triage. |

This regression run is separate from the 168 live API/store assertions above; do not combine its pytest item count into the live first-pass rate.

## API, context, job, and E2E cases

| ID | Test in English | Expected | First-pass result | Observed |
|---|---|---|---|---|
| SYS-001 | The live API reports /health. | HTTP (200,) | PASS | HTTP 200 {"status": "healthy"} |
| SYS-002 | The live API reports /health/ready. | HTTP (200,) | PASS | HTTP 200 {"status": "ready"} |
| SYS-003 | The live API publishes its OpenAPI schema. | HTTP (200,) | PASS | HTTP 200 {} |
| CONTRACT-004 | OpenAPI advertises POST /fabric-foundry/customers. | route methods ['post'] | PASS | True |
| CONTRACT-005 | OpenAPI advertises GET /fabric-foundry/industries. | route methods ['get'] | PASS | True |
| CONTRACT-006 | OpenAPI advertises GET /ingestion/customers. | route methods ['get'] | PASS | True |
| CONTRACT-007 | OpenAPI advertises GET /ingestion/context. | route methods ['get'] | PASS | True |
| CONTRACT-008 | OpenAPI advertises GET, POST /jobs. | route methods ['get', 'post'] | PASS | True |
| CONTRACT-009 | OpenAPI advertises POST /jobs/upload. | route methods ['post'] | PASS | True |
| CONTRACT-010 | OpenAPI advertises GET /jobs/{job_id}. | route methods ['get'] | PASS | True |
| CONTRACT-011 | OpenAPI advertises POST /jobs/{job_id}/resume. | route methods ['post'] | PASS | True |
| CONTRACT-012 | OpenAPI advertises POST /jobs/{job_id}/cancel. | route methods ['post'] | PASS | True |
| CONTRACT-013 | OpenAPI advertises GET /jobs/{job_id}/events. | route methods ['get'] | PASS | True |
| CONTRACT-014 | OpenAPI advertises GET /hitl/reviews. | route methods ['get'] | PASS | True |
| CONTRACT-015 | OpenAPI advertises GET /reports. | route methods ['get'] | PASS | True |
| IND-001 | The industry catalog is available. | HTTP (200,) | PASS | HTTP 200 JSON[3] |
| IND-002 | The industry catalog contains banking. | active industry entry | PASS | True |
| IND-005 | The active ontology catalog resolves for banking. | HTTP (200,) | PASS | HTTP 200 {"industry_id": "banking"} |
| IND-003 | The industry catalog contains insurance. | active industry entry | PASS | True |
| IND-006 | The active ontology catalog resolves for insurance. | HTTP (200,) | PASS | HTTP 200 {"industry_id": "insurance"} |
| IND-004 | The industry catalog contains health_insurance_care_services. | active industry entry | PASS | True |
| IND-007 | The active ontology catalog resolves for health_insurance_care_services. | HTTP (200,) | PASS | HTTP 200 {"industry_id": "health_insurance_care_services"} |
| CFG-001 | Review-policy choices load from the API. | HTTP (200,) | PASS | HTTP 200 {} |
| CFG-002 | The API offers review policy review_all. | policy option present | PASS | True |
| CFG-003 | The API offers review policy review_ambiguous. | policy option present | PASS | True |
| CFG-004 | The API offers review policy auto_write. | policy option present | PASS | True |
| AUTH-001 | Customer onboarding rejects a missing bearer token. | HTTP (401,) | PASS | HTTP 401 {"detail": "Authorization header required"} |
| AUTH-002 | Job listing rejects a missing bearer token. | HTTP (401,) | FAIL | HTTP 400 {} |
| AUTH-003 | Job detail rejects a missing bearer token. | HTTP (401,) | FAIL | HTTP 400 {} |
| AUTH-004 | Job event replay rejects a missing bearer token. | HTTP (401,) | FAIL | HTTP 400 {} |
| ONB-001 | Onboarding rejects a missing customer name. | HTTP (422,) | FAIL | HTTP 400 {} |
| ONB-002 | Onboarding rejects an unsupported industry. | HTTP (422,) | PASS | HTTP 422 {"detail": "Active industry not found: not-an-industry"} |
| ONB-003 | Onboarding rejects a blank customer name. | HTTP (422,) | FAIL | HTTP 500 {"detail": "Customer onboarding failed"} |
| ONB-004 | Authenticated onboarding rejects a malformed body. | HTTP (422,) | FAIL | HTTP 400 {} |
| ONB-005 | The API onboards a fresh banking customer without changing other customer statuses. | HTTP (201,) | PASS | HTTP 201 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "industry_id": "banking"} |
| ONB-008 | The new banking customer is active with a generated tenant identity. | ACTIVE customer with customer_id and tenant_id | PASS | True |
| ONB-011 | Onboarding creates an active version-one ontology for banking. | HTTP (200,) | PASS | HTTP 200 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "customer_ontology_version": 1} |
| ONB-014 | The banking ontology belongs to the newly onboarded customer. | customer-owned active ontology v1 | PASS | True |
| ONB-006 | The API onboards a fresh insurance customer without changing other customer statuses. | HTTP (201,) | PASS | HTTP 201 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance"} |
| ONB-009 | The new insurance customer is active with a generated tenant identity. | ACTIVE customer with customer_id and tenant_id | PASS | True |
| ONB-012 | Onboarding creates an active version-one ontology for insurance. | HTTP (200,) | PASS | HTTP 200 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "customer_ontology_version": 1} |
| ONB-015 | The insurance ontology belongs to the newly onboarded customer. | customer-owned active ontology v1 | PASS | True |
| ONB-007 | The API onboards a fresh health_insurance_care_services customer without changing other customer statuses. | HTTP (201,) | PASS | HTTP 201 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "industry_id": "health_insurance_care_services"} |
| ONB-010 | The new health_insurance_care_services customer is active with a generated tenant identity. | ACTIVE customer with customer_id and tenant_id | PASS | True |
| ONB-013 | Onboarding creates an active version-one ontology for health_insurance_care_services. | HTTP (200,) | PASS | HTTP 200 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "customer_ontology_version": 1} |
| ONB-016 | The health_insurance_care_services ontology belongs to the newly onboarded customer. | customer-owned active ontology v1 | PASS | True |
| CTX-PAGE-01 | The customer context page resolves domains for banking. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "industry_id": "banking", "customer_ontology_version": 1} |
| CTX-DOMAIN-COUNT-01 | The banking page exposes its configured domain set. | one or more domains | PASS | 4 domains |
| CTX-01-01 | The banking contextualization view loads domain corporate_commercial. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "industry_id": "banking", "customer_ontology_version": 1} |
| CTX-01-02 | The banking contextualization view loads domain consumer_retail. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "industry_id": "banking", "customer_ontology_version": 1} |
| CTX-01-03 | The banking contextualization view loads domain wealth_securities_management. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "industry_id": "banking", "customer_ontology_version": 1} |
| CTX-01-04 | The banking contextualization view loads domain risk_compliance_fraud. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "industry_id": "banking", "customer_ontology_version": 1} |
| CTX-INVALID-01 | The customer context API rejects an unknown domain for banking. | HTTP (404,) | PASS | HTTP 404 {"detail": {"code": "CONTEXTUALIZATION_NOT_FOUND", "message": "Domain not found in ontology: not-a-domain-live-20261013-fifthpass", "details": {"customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fift… |
| CTX-SELECT-01 | The target domain contains canonical product lines for banking. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "industry_id": "banking", "customer_ontology_version": 1} |
| CTX-SAVE-01 | One complete applicability batch configures Retail Cards for banking. | HTTP (200,) | PASS | HTTP 200 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "customer_ontology_version": 2} |
| CTX-VERSION-01 | Contextualization creates customer ontology version two for banking. | active version 2 | PASS | True |
| CTX-IDEMPOTENCY-01 | Replaying the same contextualization request is idempotent for banking. | HTTP (200,) | PASS | HTTP 200 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "customer_ontology_version": 2} |
| CTX-IDEMPOTENCY-VERSION-01 | Contextualization replay does not create another version for banking. | still version 2 | PASS | True |
| CTX-STALE-01 | A stale base ontology version is rejected for banking. | HTTP (400,) | PASS | HTTP 400 {"detail": {"code": "CONTEXTUALIZATION_INVALID", "message": "Customer Ontology version conflict: expected 1, current 2", "details": {"customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae63… |
| CTX-HISTORY-01 | Ontology history for banking records versions newest first. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc"} |
| CTX-HISTORY-ORDER-01 | Ontology history contains versions 2 then 1 for banking. | [2, 1] version order | PASS | True |
| CTX-INGESTION-01 | Ingestion bootstrap returns the configured product line for banking. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "industry_id": "banking", "customer_ontology_version": 2} |
| CTX-INGESTION-PL-01 | The selected product line is present for banking ingestion. | Retail Cards | PASS | True |
| CTX-PAGE-02 | The customer context page resolves domains for insurance. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance", "customer_ontology_version": 1} |
| CTX-DOMAIN-COUNT-02 | The insurance page exposes its configured domain set. | one or more domains | PASS | 7 domains |
| CTX-02-01 | The insurance contextualization view loads domain property_casualty. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance", "customer_ontology_version": 1} |
| CTX-02-02 | The insurance contextualization view loads domain life_insurance. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance", "customer_ontology_version": 1} |
| CTX-02-03 | The insurance contextualization view loads domain annuities. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance", "customer_ontology_version": 1} |
| CTX-02-04 | The insurance contextualization view loads domain health_insurance. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance", "customer_ontology_version": 1} |
| CTX-02-05 | The insurance contextualization view loads domain retirement_workplace_benefits. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance", "customer_ontology_version": 1} |
| CTX-02-06 | The insurance contextualization view loads domain employee_benefits. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance", "customer_ontology_version": 1} |
| CTX-02-07 | The insurance contextualization view loads domain insurance_operations_services. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance", "customer_ontology_version": 1} |
| CTX-INVALID-02 | The customer context API rejects an unknown domain for insurance. | HTTP (404,) | PASS | HTTP 404 {"detail": {"code": "CONTEXTUALIZATION_NOT_FOUND", "message": "Domain not found in ontology: not-a-domain-live-20261013-fifthpass", "details": {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fi… |
| CTX-SELECT-02 | The target domain contains canonical product lines for insurance. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance", "customer_ontology_version": 1} |
| CTX-SAVE-02 | One complete applicability batch configures Individual & Family Health for insurance. | HTTP (200,) | PASS | HTTP 200 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "customer_ontology_version": 2} |
| CTX-VERSION-02 | Contextualization creates customer ontology version two for insurance. | active version 2 | PASS | True |
| CTX-IDEMPOTENCY-02 | Replaying the same contextualization request is idempotent for insurance. | HTTP (200,) | PASS | HTTP 200 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "customer_ontology_version": 2} |
| CTX-IDEMPOTENCY-VERSION-02 | Contextualization replay does not create another version for insurance. | still version 2 | PASS | True |
| CTX-STALE-02 | A stale base ontology version is rejected for insurance. | HTTP (400,) | PASS | HTTP 400 {"detail": {"code": "CONTEXTUALIZATION_INVALID", "message": "Customer Ontology version conflict: expected 1, current 2", "details": {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a… |
| CTX-HISTORY-02 | Ontology history for insurance records versions newest first. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284"} |
| CTX-HISTORY-ORDER-02 | Ontology history contains versions 2 then 1 for insurance. | [2, 1] version order | PASS | True |
| CTX-INGESTION-02 | Ingestion bootstrap returns the configured product line for insurance. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "industry_id": "insurance", "customer_ontology_version": 2} |
| CTX-INGESTION-PL-02 | The selected product line is present for insurance ingestion. | Individual & Family Health | PASS | True |
| CTX-PAGE-03 | The customer context page resolves domains for health_insurance_care_services. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "industry_id": "health_insurance_care_services", "customer_ontology_version": 1} |
| CTX-DOMAIN-COUNT-03 | The health_insurance_care_services page exposes its configured domain set. | one or more domains | PASS | 3 domains |
| CTX-03-01 | The health_insurance_care_services contextualization view loads domain health_insurance. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "industry_id": "health_insurance_care_services", "customer_ontology_version": 1} |
| CTX-03-02 | The health_insurance_care_services contextualization view loads domain care. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "industry_id": "health_insurance_care_services", "customer_ontology_version": 1} |
| CTX-03-03 | The health_insurance_care_services contextualization view loads domain pharmacy. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "industry_id": "health_insurance_care_services", "customer_ontology_version": 1} |
| CTX-INVALID-03 | The customer context API rejects an unknown domain for health_insurance_care_services. | HTTP (404,) | PASS | HTTP 404 {"detail": {"code": "CONTEXTUALIZATION_NOT_FOUND", "message": "Domain not found in ontology: not-a-domain-live-20261013-fifthpass", "details": {"customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-f… |
| CTX-SELECT-03 | The target domain contains canonical product lines for health_insurance_care_services. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "industry_id": "health_insurance_care_services", "customer_ontology_version": 1} |
| CTX-SAVE-03 | One complete applicability batch configures Care Management for health_insurance_care_services. | HTTP (200,) | PASS | HTTP 200 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "customer_ontology_version": 2} |
| CTX-VERSION-03 | Contextualization creates customer ontology version two for health_insurance_care_services. | active version 2 | PASS | True |
| CTX-IDEMPOTENCY-03 | Replaying the same contextualization request is idempotent for health_insurance_care_services. | HTTP (200,) | PASS | HTTP 200 {"status": "ACTIVE", "customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "customer_ontology_version": 2} |
| CTX-IDEMPOTENCY-VERSION-03 | Contextualization replay does not create another version for health_insurance_care_services. | still version 2 | PASS | True |
| CTX-STALE-03 | A stale base ontology version is rejected for health_insurance_care_services. | HTTP (400,) | PASS | HTTP 400 {"detail": {"code": "CONTEXTUALIZATION_INVALID", "message": "Customer Ontology version conflict: expected 1, current 2", "details": {"customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2… |
| CTX-HISTORY-03 | Ontology history for health_insurance_care_services records versions newest first. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89"} |
| CTX-HISTORY-ORDER-03 | Ontology history contains versions 2 then 1 for health_insurance_care_services. | [2, 1] version order | PASS | True |
| CTX-INGESTION-03 | Ingestion bootstrap returns the configured product line for health_insurance_care_services. | HTTP (200,) | PASS | HTTP 200 {"customer_id": "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "industry_id": "health_insurance_care_services", "customer_ontology_version": 2} |
| CTX-INGESTION-PL-03 | The selected product line is present for health_insurance_care_services ingestion. | Care Management | PASS | True |
| JOB-LIST-001 | An authenticated user can list their visible jobs. | HTTP (200,) | PASS | HTTP 200 {} |
| JOB-LIST-002 | Job listing accepts filter/pagination case {'limit': 1}. | HTTP (200,) | PASS | HTTP 200 {} |
| JOB-LIST-003 | Job listing accepts filter/pagination case {'limit': 50}. | HTTP (200,) | PASS | HTTP 200 {} |
| JOB-LIST-004 | Job listing accepts filter/pagination case {'limit': 200}. | HTTP (200,) | PASS | HTTP 200 {} |
| JOB-LIST-005 | Job listing accepts filter/pagination case {'limit': 2, 'offset': 1}. | HTTP (200,) | PASS | HTTP 200 {} |
| JOB-LIST-006 | Job listing accepts filter/pagination case {'status': 'FAILED'}. | HTTP (200,) | PASS | HTTP 200 {} |
| JOB-LIST-007 | Job listing accepts filter/pagination case {'status': 'COMPLETED'}. | HTTP (200,) | PASS | HTTP 200 {} |
| JOB-LIST-008 | Job listing accepts filter/pagination case {'customer_id': 'missing-customer'}. | HTTP (200,) | PASS | HTTP 200 {} |
| JOB-LIST-009 | Job listing accepts filter/pagination case {'domain_id': 'consumer_retail'}. | HTTP (200,) | PASS | HTTP 200 {} |
| JOB-LIST-010 | Job listing accepts filter/pagination case {'product_line_id': 'missing-product'}. | HTTP (200,) | PASS | HTTP 200 {} |
| JOB-LIST-INVALID-001 | Job listing rejects a zero page size. | HTTP (400,) | PASS | HTTP 400 {} |
| JOB-LIST-INVALID-002 | Job listing rejects an excessive page size. | HTTP (400,) | PASS | HTTP 400 {} |
| JOB-DETAIL-UNKNOWN | Unknown job details return not found. | HTTP (404,) | PASS | HTTP 404 {"detail": "Job not found"} |
| JOB-LOGS-UNKNOWN | Unknown job logs return not found or safe service error. | HTTP (404, 500) | PASS | HTTP 500 {"detail": "Failed to get logs: Request failed with status code 404: Job no-such-live-job does not exist."} |
| JOB-PROGRESS-UNKNOWN | Unknown job progress returns not found. | HTTP (404,) | PASS | HTTP 404 {"detail": "No progress file for job: no-such-live-job"} |
| JOB-EVENTS-UNKNOWN | Unknown job event replay returns not found. | HTTP (404,) | PASS | HTTP 404 {"detail": "Job not found"} |
| JOB-CANCEL-UNKNOWN | Cancelling an unknown job returns not found. | HTTP (404,) | PASS | HTTP 404 {"detail": "Job not found"} |
| JOB-RESUME-UNKNOWN | Resuming an unknown job returns not found. | HTTP (404,) | PASS | HTTP 404 {"detail": "Job not found"} |
| JOB-RESUME-IDEMPOTENCY-REQUIRED | Resume requires an idempotency key. | HTTP (400,) | PASS | HTTP 400 {} |
| JOB-SUBMIT-INVALID | Directory submission rejects an incomplete scope before dispatch. | HTTP (400,) | PASS | HTTP 400 {} |
| REPORT-LIST-001 | The report listing endpoint responds. | HTTP (200,) | PASS | HTTP 200 {} |
| REPORT-UNKNOWN | A report lookup for a missing job is not found. | HTTP (404,) | PASS | HTTP 404 {"detail": "Report not found for job: no-such-live-job"} |
| HITL-POLICY-SCOPE | Review policy endpoint exposes the applicable review modes. | HTTP (200,) | PASS | HTTP 200 {} |
| HITL-LIST-01 | HITL review listing handles scope combination 1. | HTTP (400,) | PASS | HTTP 400 {} |
| HITL-LIST-02 | HITL review listing handles scope combination 2. | HTTP (400,) | PASS | HTTP 400 {} |
| HITL-LIST-03 | HITL review listing handles scope combination 3. | HTTP (400,) | PASS | HTTP 400 {} |
| HITL-LIST-04 | HITL review listing handles scope combination 4. | HTTP (200,) | PASS | HTTP 200 {} |
| HITL-ITEM-UNKNOWN | A review lookup for a missing item is not found. | HTTP (404,) | PASS | HTTP 404 {"detail": "HITL review item not found in supplied scope"} |
| METRICS-001 | The API metrics endpoint responds. | HTTP (200,) | PASS | HTTP 200 '# HELP python_gc_objects_collected_total Objects collected during gc\n# TYPE python_gc_objects_collected_total counter\npython_gc_objects_collected_total{generati' |
| CONFIG-001 | The ingestion configuration endpoint responds. | HTTP (200,) | PASS | HTTP 200 {} |
| E2E-UPLOAD-01 | A synthetic banking document is uploaded under its explicit customer/domain/product line. | HTTP (200,) | PASS | HTTP 200 {"status": "submitted", "job_id": "ingest-consumer_retail-20261013-141351-59dc"} |
| E2E-TERMINAL-01 | The banking ingestion job reaches a terminal state. | terminal by 5 minutes | PASS | status=COMPLETED job_id=ingest-consumer_retail-20261013-141351-59dc |
| E2E-STAGES-01 | The banking job details expose persisted stage evidence. | stage array present | PASS | 15 stages |
| E2E-REPORT-01 | The banking report matches its terminal job outcome. | HTTP (200,) | PASS | HTTP 200 '<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>Pulse Knowled' |
| E2E-EVENTS-01 | The banking run opens persisted event replay. | HTTP 200 SSE stream | PASS | 200 |
| E2E-UPLOAD-02 | A synthetic insurance document is uploaded under its explicit customer/domain/product line. | HTTP (200,) | PASS | HTTP 200 {"status": "submitted", "job_id": "ingest-health_insurance-20261013-141816-4e08"} |
| E2E-TERMINAL-02 | The insurance ingestion job reaches a terminal state. | terminal by 5 minutes | PASS | status=FAILED job_id=ingest-health_insurance-20261013-141816-4e08 |
| E2E-STAGES-02 | The insurance job details expose persisted stage evidence. | stage array present | PASS | 15 stages |
| E2E-REPORT-02 | The insurance report matches its terminal job outcome. | HTTP (404,) | FAIL | HTTP 200 '<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>Pulse Knowled' |
| E2E-EVENTS-02 | The insurance run opens persisted event replay. | HTTP 200 SSE stream | PASS | 200 |
| E2E-UPLOAD-03 | A synthetic health_insurance_care_services document is uploaded under its explicit customer/domain/product line. | HTTP (200,) | PASS | HTTP 200 {"status": "submitted", "job_id": "ingest-care-20261013-141925-5de7"} |
| E2E-TERMINAL-03 | The health_insurance_care_services ingestion job reaches a terminal state. | terminal by 5 minutes | PASS | status=FAILED job_id=ingest-care-20261013-141925-5de7 |
| E2E-STAGES-03 | The health_insurance_care_services job details expose persisted stage evidence. | stage array present | PASS | 15 stages |
| E2E-REPORT-03 | The health_insurance_care_services report matches its terminal job outcome. | HTTP (404,) | FAIL | HTTP 200 '<!DOCTYPE html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n  <title>Pulse Knowled' |
| E2E-EVENTS-03 | The health_insurance_care_services run opens persisted event replay. | HTTP 200 SSE stream | PASS | 200 |

## Direct PostgreSQL, Weaviate, and Neo4j cases

| ID | Test in English | Expected | Result | Observed |
|---|---|---|---|---|
| STORE-01-PG-RUN | PostgreSQL retains the submitted run scope and actor. | run exists and actor/scope/version match API detail | PASS | ["COMPLETED", "test-live-validation-20261013", "dev_test_token", "Pulse AI_KFabric API QA Banking-live-20261013-fifthpass_cae635c0-9e3f-49f0-ad9a-e33e593e12dc", "banking", "consumer_retail", "DOMAIN:consumer_retail\|PRODUCT_LINE:Retail Cards", 2] |
| STORE-01-PG-STAGES | PostgreSQL retains all 15 terminal stage rows. | 15 rows and zero RUNNING/RETRY_WAIT rows | PASS | {"rows": 15, "active": 0} |
| STORE-01-PG-EVENTS | Lifecycle/progress events are persisted for replay. | at least one event | PASS | 55 |
| STORE-01-PG-CHECKPOINTS | Recoverable worker checkpoints are persisted. | at least one immutable checkpoint | PASS | 4 |
| STORE-01-PG-STORE-LEDGER | Store outcomes are independently finalized in PostgreSQL. | both stores have only SUCCEEDED, FAILED, or SKIPPED units | FAIL | [["weaviate", "SUCCEEDED", 1]] |
| STORE-01-WEAVIATE-BATCH | Weaviate contains a batch-scoped document block. | one or more objects tagged with this ingestion batch | PASS | {"count": 1, "errors": null} |
| STORE-01-NEO4J-BATCH | Neo4j contains run-scoped graph writes. | at least one node or relationship tagged with this ingestion batch | FAIL | {"nodes": 0, "relationships": 0} |
| STORE-01-REPORT | A run report is discoverable for completed or failed processing. | HTTP 200 for terminal run | PASS | 200 |
| STORE-02-PG-RUN | PostgreSQL retains the submitted run scope and actor. | run exists and actor/scope/version match API detail | PASS | ["FAILED", "test-live-validation-20261013", "dev_test_token", "Pulse AI_KFabric API QA Insurance-live-20261013-fifthpass_45a551ac-ec92-46c3-80bd-0dac66407284", "insurance", "health_insurance", "DOMAIN:health_insurance\|PRODUCT_LINE:Individual & Family Health"… |
| STORE-02-PG-STAGES | PostgreSQL retains all 15 terminal stage rows. | 15 rows and zero RUNNING/RETRY_WAIT rows | PASS | {"rows": 15, "active": 0} |
| STORE-02-PG-EVENTS | Lifecycle/progress events are persisted for replay. | at least one event | PASS | 51 |
| STORE-02-PG-CHECKPOINTS | Recoverable worker checkpoints are persisted. | at least one immutable checkpoint | PASS | 4 |
| STORE-02-PG-STORE-LEDGER | Store outcomes are independently finalized in PostgreSQL. | both stores have only SUCCEEDED, FAILED, or SKIPPED units | FAIL | [["weaviate", "IN_PROGRESS", 1]] |
| STORE-02-WEAVIATE-BATCH | Weaviate contains a batch-scoped document block. | one or more objects tagged with this ingestion batch | FAIL | {"count": null, "errors": [{"locations": [{"column": 15, "line": 1}], "message": "Cannot query field \"Health_insurance\" on type \"AggregateObjectsObj\".", "path": null}]} |
| STORE-02-NEO4J-BATCH | Neo4j contains run-scoped graph writes. | at least one node or relationship tagged with this ingestion batch | FAIL | {"nodes": 0, "relationships": 0} |
| STORE-02-REPORT | A run report is discoverable for completed or failed processing. | HTTP 200 for terminal run | PASS | 200 |
| STORE-03-PG-RUN | PostgreSQL retains the submitted run scope and actor. | run exists and actor/scope/version match API detail | PASS | ["FAILED", "test-live-validation-20261013", "dev_test_token", "Pulse AI_KFabric API QA Healthcare-live-20261013-fifthpass_c2022b0f-5a1d-4a82-8305-055dc5806e89", "health_insurance_care_services", "care", "DOMAIN:care\|PRODUCT_LINE:Care Management", 2] |
| STORE-03-PG-STAGES | PostgreSQL retains all 15 terminal stage rows. | 15 rows and zero RUNNING/RETRY_WAIT rows | PASS | {"rows": 15, "active": 0} |
| STORE-03-PG-EVENTS | Lifecycle/progress events are persisted for replay. | at least one event | PASS | 51 |
| STORE-03-PG-CHECKPOINTS | Recoverable worker checkpoints are persisted. | at least one immutable checkpoint | PASS | 4 |
| STORE-03-PG-STORE-LEDGER | Store outcomes are independently finalized in PostgreSQL. | both stores have only SUCCEEDED, FAILED, or SKIPPED units | FAIL | [["weaviate", "IN_PROGRESS", 1]] |
| STORE-03-WEAVIATE-BATCH | Weaviate contains a batch-scoped document block. | one or more objects tagged with this ingestion batch | FAIL | {"count": null, "errors": [{"locations": [{"column": 15, "line": 1}], "message": "Cannot query field \"Care\" on type \"AggregateObjectsObj\".", "path": null}]} |
| STORE-03-NEO4J-BATCH | Neo4j contains run-scoped graph writes. | at least one node or relationship tagged with this ingestion batch | FAIL | {"nodes": 0, "relationships": 0} |
| STORE-03-REPORT | A run report is discoverable for completed or failed processing. | HTTP 200 for terminal run | PASS | 200 |

## Evidence files

- API suite: `reports/live_validation/live-20261013-fifthpass.json`
- Direct store verification: `reports/live_validation/live-20261013-fifthpass-store-verification.json`

## What the live run did not establish

- A run-scoped Neo4j node or relationship write; batch-scoped queries returned zero for all three runs.
- Successful insurance or healthcare publishing; both encountered banking-only Weaviate class configuration and retained nonterminal store-ledger state.
- OTEL export or Langfuse capture; the configured OTEL collector was unreachable at API startup.
- Cancel/resume success paths, HITL review decisions, retry recovery, and concurrency behavior; only unknown-job and request-shape branches were exercised in this live batch.

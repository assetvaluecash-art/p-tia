# Sample test repos: Northwind Pay

A small, complete landscape to try every TIA feature without real code: five repositories of a made-up bank, the testers' 14 written test
cases, and six prepared changes with the result TIA should give for each. The same landscape and expected results are checked on every
test run of TIA (`development/tests/test_showcase.py`), so what you see should match exactly.

This folder is a sample, not part of the server: the container image is built from `tia/` only and does not include it.

## What is in this folder

| Path | What it is |
|---|---|
| `repos/` | The five repositories as plain source (their `main` version): payments-api, statement-service, mobile-bff, web-portal, qa-testcases |
| `zips/base/` | One zip per repository, to upload in the web UI |
| `zips/scenarios/NN-name/payments-api.zip` | payments-api after one change, to upload over the base version |
| `evidence/` | CI test results (JUnit), observed runtime calls, an incident, and a note on deployments: what a pipeline would send TIA |
| `expected.json` | The expected result of every scenario, machine-readable |
| `make_git_repos.py` | Turns `repos/` into git repositories with the six changes as branches of payments-api (for the command line) |

## Two ways in

**Web UI (no git needed).** In section *1. Repositories -> Upload zip*, upload the five zips of `zips/base/`, one at a time; the repository
name is the file name. Then for a scenario: upload its `payments-api.zip` under the same name, press *Analyse vs previous upload*, then
*Run analysis*. Before the next scenario, upload `zips/base/payments-api.zip` again so each change is compared with the original.

**Command line or register by path.** Run `python make_git_repos.py` (needs git), then point TIA at the repositories in `git-repos/`:

```
tia analyze --repo payments-api=git-repos/payments-api --repo statement-service=git-repos/statement-service \
    --repo mobile-bff=git-repos/mobile-bff --repo web-portal=git-repos/web-portal --repo qa-testcases=git-repos/qa-testcases \
    --target payments-api --base main --head feature/holds
```

## Optional: the evidence

The scenarios work without it. The tabs *Releases*, *What ran where* and *Incidents*, and the CI results shown next to tests, need it. It is
loaded through TIA's evidence API (`POST /api/v1/evidence/...`, see the API reference). In a development checkout,
`python development/tools/showcase.py --out showcase --load <TIA address>` uploads the base zips and loads all of it in one step.

## The six scenarios

| # | Upload (then *Analyse vs previous upload*) | The change | What TIA should tell you |
|---|---|---|---|
| 1 | `zips/scenarios/01-feature-holds/payments-api.zip` (branch `feature/holds`) | balance_of() subtracts held amounts when the new environment variable HOLDS_ENABLED is true | Regression-test 5 of your 14. Configuration: balance_of now reads HOLDS_ENABLED, no file sets it (set it in Dev); held_amount already reads the same key. Test cases to rerun: NW-01, NW-02, NW-03, NW-07, NW-08. |
| 2 | `zips/scenarios/02-feature-min-balance/payments-api.zip` (branch `feature/min-balance`) | config/app.env raises MIN_BALANCE from 80 to 100 (configuration only) | Regression-test 3 of your 14. Configuration: MIN_BALANCE changes value in config/app.env, read by can_pay. No value is shown. Test cases to rerun: NW-01, NW-02, NW-03. |
| 3 | `zips/scenarios/03-feature-fee-change/payments-api.zip` (branch `feature/fee-change`) | fee_for() charges 1.5% instead of 1% (logic change) | Regression-test 4 of your 14: making a payment and refunding one (both charge the fee). Test cases to rerun: NW-01, NW-02, NW-03, NW-09. |
| 4 | `zips/scenarios/04-feature-remove-disputed/payments-api.zip` (branch `feature/remove-disputed`) | the `disputed` field is removed from PaymentRow, the response of the shared endpoint (breaking contract) | BREAKING: PaymentRow.disputed removed. Critical finding on statement-service, high on mobile-bff. A rule gap (statement-service leaves out disputed payments, mobile-bff does not) and a consistency check. Regression-test 4 of your 14. Test cases to rerun: NW-06, NW-08, NW-11, NW-12. |
| 5 | `zips/scenarios/05-feature-drop-reference/payments-api.zip` (branch `feature/drop-reference`) | an Alembic migration drops the payments.reference column that mobile-bff still reads (breaking data change) | BREAKING data change: column payments.reference dropped while payments-api and mobile-bff (same database) still map it. Status PARTIAL on purpose: the walk reaches the depth limit (8), stated under Unknowns; raise max_depth in section 6 to follow it further. Regression-test 6 of your 14. Test cases to rerun: NW-04, NW-05, NW-06, NW-08, NW-09, NW-12. |
| 6 | `zips/scenarios/06-docs-clarify/payments-api.zip` (branch `docs/clarify`) | a docstring is reworded (no functional change) | Documentation only: nothing to retest (Regression-test 0 of your 14). Test cases to rerun: none. |

NW-14 (*Archive export reaches the archive*) names nothing TIA can match, so every scenario lists it under *review by hand*.
NW-13 (*Sign in*) tests the /login screen, which calls no API, so API changes never select it.

## Walk through every section of the web UI

Open your TIA (for a local one: `http://127.0.0.1:8080`) and follow the sections in order. The rows marked *needs the evidence* show data
only after the files in `evidence/` are loaded (see *Optional: the evidence* above).

| Section | What to do | What you should see |
|---|---|---|
| **1. Repositories → Upload zip** | Upload the five zips of `zips/base/`, one at a time; the repository name is the file name (payments-api, statement-service, mobile-bff, web-portal, qa-testcases). | Five repositories of kind *upload*. |
| **2. Code graph** | Level *service*. Then search `payments` at level *function*. | payments-api with statement-service, mobile-bff and web-portal calling it; the topic `payments.created` consumed by statement-service. |
| **2. Ask the graph** | *Consumers of* `GET /accounts/{account_id}/payments`. | statement-service, mobile-bff and web-portal. |
| **3. Analyse a change** | For each scenario below: upload its `payments-api.zip` (same name, payments-api), press **Analyse vs previous upload**, then **Run analysis**. Before the next scenario, upload `zips/base/payments-api.zip` again so the next change is compared with the original. | The result in the scenario table. |
| **4. Report → What to test** | Open any scenario's report. | First **Regression-test N of your 14 test case(s)** with the reason per test case, NW-14 under *review by hand*, then the entry points, rule gaps, consistency and configuration checks. |
| **4. Report → Tester pack** | Press *Tester pack* on scenario 1. | The coverage gate, the API test matrix of `POST /payments` and the locator pack of the Pay screen. |
| **5. Tester views → Common components & rule gaps** | Open the tab. | `GET /accounts/{account_id}/payments` is shared; rule gap: statement-service leaves out disputed payments, mobile-bff does not. |
| **5. Tester views → Technical coverage** | Open the tab. | Every API and screen with the aspects the tests and the 14 written cases cover, and the gaps. |
| **5. Tester views → Automation: APIs / pages & locators** | Open both tabs. | Every endpoint's inputs, rules and valid/invalid values; the four screens with their elements and locators. |
| **5. Tester views → Check one test case** | Paste NW-02's title, entry point (`POST /payments`) and expected result. | Which technical aspects it covers (the 409 refusal). |
| **5. Tester views → Execution flow** | Entry point `POST /payments`, then *Show execution flow* (API: `GET /api/v1/graph/flows`; CLI: `tia flows`). | `api_create_payment [controller]` → `create_payment [service]` → `load_account` / `save_payment [data access]` and `publish_created [events]` → topic `payments.created`; below it the design of each service: controllers, models, entities. |
| **5. Tester views → Automation script** | Repository `web-portal`, from `/login` to `/done`, then *Generate script* (API: `POST /api/v1/automation/script`; CLI: `tia script`). | A Playwright test through /login, /accounts, /pay and /done that fills every input with a valid value and waits for `POST /payments` (201); nothing left to review. |
| **5a. Releases, What ran where, Incidents** | Open the three tabs (needs the evidence). | Release *2026.11-showcase*; payments-api in staging and production; incident INC-2001 on statement-service. |
| **5b. Insights** | Open the section after a few analyses. | Trends of the scenarios you ran. |
| **6. Configuration** | Raise *max_depth* to 12, then rerun scenario 5. | Its status changes from *partial* to *complete_with_gaps*. |
| **Neo4j** (optional) | With `NEO4J_URI`/`NEO4J_PASSWORD` set, `POST /api/v1/graph/neo4j-sync`. | The landscape, the 14 test cases and the screens in Neo4j (docs/NEO4J.md has the Cypher). |

## The written test cases

| ID | Title |
|---|---|
| NW-01 | Create a payment |
| NW-02 | Payment refused below the minimum balance |
| NW-03 | Pay screen rejects an amount above the limit |
| NW-04 | View a payment |
| NW-05 | Unknown payment |
| NW-06 | Payment history |
| NW-07 | Account balance |
| NW-08 | Accounts screen shows the balance |
| NW-09 | Refund a payment |
| NW-10 | Exchange rates |
| NW-11 | Statement leaves out disputed payments |
| NW-12 | Mobile activity shows every payment |
| NW-13 | Sign in |
| NW-14 | Archive export reaches the archive |

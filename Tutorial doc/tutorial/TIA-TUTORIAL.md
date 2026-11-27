# TIA, explained: a step-by-step guide with one running example

This guide explains what TIA does and how it does it, from the first look at your code to the comment it writes on a pull request. It follows one small example from start to finish. Every output you see in it was produced by running TIA on that example while this guide was built, so what you read is what the program really says.

**Who it is for.** Testers, developers and managers who are new to TIA. You do not need to read code to follow the first nine chapters; where code appears, the text says in words what it does. Chapters 12 to 14 are for people who will work on TIA itself.

**Prefer a picture?** [The pipeline page](index.html) shows the same steps left to right; click any step to open its business logic, code logic, a real example and ideas for the future.

**How to read it.** Read chapters 1 to 9 in order: each builds on the one before. The rest can be read in any order. Words in *italics* are explained in the glossary at the end.

**One note on the examples.** A *commit* (a saved version of the code) has an ID that is different on every computer, so those IDs are shown as `<commit>`. Nothing else in the examples has been changed.

---

## 1. The problem TIA solves

**In this chapter:** why a small change in one program can break another, and the question TIA answers.

Large companies do not run one big program. They run many smaller programs, called *services*, that ask each other for information. A bank might have one service that keeps the list of payments, another that prints the monthly statement, and another that shows payments on the customer's screen. The statement service and the screen do not keep their own copy of the payments: they ask the payments service every time.

That is efficient, but it creates a risk. Imagine a developer on the payments team removes one piece of information that the payments service sends out. Their own tests pass, because their own service still works. But the statement service relied on that piece of information. Next month, statements are wrong, and nobody connected the two events.

Before a change goes live, someone has to answer three questions:

1. **What does this change affect?** Not only in the service that changed, but in every service that depends on it.
2. **How serious is it?** Will something stop working, or will it just behave a little differently?
3. **What must we test?** Which screens, reports and tests should be checked before release?

Today, people answer these by asking around and by memory. TIA answers them from the code itself. It reads every service's code, works out which service asks which other service for what, and when someone proposes a change, it follows that change to everything it can reach.

**What TIA does not do.** It does not run your programs, and it does not guess. It reads the code as text. When it cannot prove a connection, it says so instead of making one up. Chapter 9 is about exactly that.

**In short**

- Services depend on each other, so a change in one can break another.
- TIA answers: what does this change affect, how serious is it, and what must we test.
- It works by reading the code, never by running it or guessing.

## 2. The example used in this guide

**In this chapter:** the three small services we follow through the whole guide, and the change we will analyse.

Our example is a tiny bank with three services. Each one lives in its own *repository* (a folder of code with its full history, kept by a tool called git).

| Service | Its addresses (what other programs can ask it for) | Files TIA read |
|---|---|---|
| payments-service | `GET /accounts/{account_id}/payments`, `POST /accounts/{account_id}/payments` | 2 |
| statement-service | `GET /statements/{account_id}/pdf` | 6 |
| customer-portal | `GET /portal/accounts/{account_id}/payments`, `POST /portal/accounts/{account_id}/payments` | 7 |

The middle column lists each service's *addresses*. An address is what another program asks for, written as an action and a path, for example `GET /accounts/{account_id}/payments`: "get the payments of this account". Developers call these *endpoints*; this guide says "address".

**The payments service** keeps the payments. Here is the part of its code that describes one payment, and the address that hands out the list. The numbers on the left are line numbers in the file.

```python
12  class Payment(BaseModel):
13      payment_id: str
14      status: str
15      amount: float
16      disputed: bool
...
47  @app.get("/accounts/{account_id}/payments", response_model=List[Payment])
48  def list_payments(account_id: str, status: Optional[PaymentStatus] = None):
49      """Payments of one account."""
50      if account_id not in ACCOUNTS:
51          raise HTTPException(status_code=404, detail="unknown account")
52      return ordered(load_payments(account_id))
```

In words: a payment has four pieces of information, an ID, a status, an amount, and **`disputed`**, which says whether the customer has challenged the payment. The address `GET /accounts/{account_id}/payments` sends back a list of those payments.

**The statement service** prints the monthly statement as a PDF. It asks the payments service for the payments with this small piece of code:

```python
5  PAYMENTS_BASE_URL = os.getenv("PAYMENTS_BASE_URL", "http://payments-service")
6  
7  
8  def fetch_payments(account_id: str):
9      return requests.get(f"{PAYMENTS_BASE_URL}/accounts/{account_id}/payments", timeout=5).json()
```

In words: it reads a setting called `PAYMENTS_BASE_URL`, which holds where the payments service can be reached, and asks that address for an account's payments.

Then it applies the bank's rule: **disputed payments do not appear on the statement.**

```python
 8  def statement_rows(account_id: str):
 9      payments = fetch_payments(account_id)
10      return [p for p in payments if not p["disputed"]]
```

Line 10 keeps only the payments that are not disputed. Remember this line: it matters later.

**The customer portal** shows payments on the customer's screen. It asks the payments service in the same way, and shows every payment it gets back:

```python
15  @app.get("/portal/accounts/{account_id}/payments")
16  def account_payments(account_id: str):
17      payments = fetch_payments(account_id)
18      return [{"id": p["payment_id"], "amount": p["amount"], "status": p["status"]} for p in payments]
```

Notice that the portal does **not** leave out disputed payments. Whether that is right is a business question, and in chapter 8 TIA raises it.

**How TIA learns who talks to whom.** Each service has a small settings file called `tia-services.yaml`. This is the statement service's:

```yaml
service: statement-service
owner: team-statements
config_keys:
  PAYMENTS_BASE_URL: payments-service
entry_points:
  GET /statements/{account_id}/pdf: Monthly statement PDF
```

It says three things in plain terms: the service's name and owning team; that the setting `PAYMENTS_BASE_URL` points at the payments service; and that the address `GET /statements/{account_id}/pdf` is the "Monthly statement PDF", a name a tester will recognise.

**The change we will analyse.** A developer on the payments team deletes the `disputed` field. This is the whole change, shown as a *diff* (lines starting with `-` were removed):

```diff
--- app.py (before)
+++ app.py (after)
@@ -13,7 +13,6 @@
     payment_id: str
     status: str
     amount: float
-    disputed: bool
 
 
 class Currency(str, Enum):
```

One line removed. The payments team's own tests still pass. The question for TIA: what else does this break?

**In short**

- Three services: payments (the data), statement (a PDF that leaves out disputed payments), portal (a screen that shows all payments).
- Both the statement and the portal get their data from the payments service.
- The change: the payments service stops sending `disputed`.

## 3. Step one: reading the code

**In this chapter:** what TIA finds when it reads each repository, and why it never runs the code.

TIA's first job is to read every file and write down the facts it needs. It does this for each language it knows (Python, Java, Go, C#, Kotlin, TypeScript and JavaScript, plus web pages, database files and written test cases). It reads files as text. **It never runs, installs or builds your code**, so a broken or even malicious repository cannot do anything inside TIA.

Here is what TIA found in the payments service.

**Its addresses**, which function answers each one, and where in the code:

| Address | Answered by the function | Where | Sends back |
|---|---|---|---|
| `GET /accounts/{account_id}/payments` | `list_payments` | app.py line 48 | Payment |
| `POST /accounts/{account_id}/payments` | `create_payment` | app.py line 61 | not declared |

**Its data shapes** (developers say *schemas*): the structure of the information it sends and receives:

| Data shape | Fields |
|---|---|
| `Payment` | payment_id, status, amount, disputed |
| `PaymentRequest` | amount, currency, reference, express, tags |

**Its tests**, and which address each test calls:

| Test | File | What it calls |
|---|---|---|
| `test_create_payment` | tests/test_payments.py | POST /accounts/123/payments |
| `test_create_payment_requires_a_reference` | tests/test_payments.py | POST /accounts/123/payments |
| `test_list_payments` | tests/test_payments.py | GET /accounts/123/payments |
| `test_unknown_account_is_404` | tests/test_payments.py | GET /accounts/999/payments |

In the statement service, TIA also found the places where the code **calls out** to another program:

| Call made by | Asks for | Address taken from | Where |
|---|---|---|---|
| `fetch_payments` | `GET /accounts/{}/payments` | setting `PAYMENTS_BASE_URL` | client.py line 9 |
| `StatementRequest` | `GET /statements/{}/pdf` | nothing: the page calls its own server | web/src/StatementRequest.jsx line 8 |
| `download` | `GET /statements/{}/pdf` | nothing: the page calls its own server | web/src/StatementRequest.jsx line 8 |

The first row is the call we saw in chapter 2: `fetch_payments` asks for `GET /accounts/{}/payments` at the address held in the setting `PAYMENTS_BASE_URL`. (TIA writes `{}` for any part of a path that changes, such as an account number.) The other two rows are the statement's web page asking its own server for the PDF.

TIA also reads **written test cases**, the ones testers keep in spreadsheets or files, so it can tell you later which of them a change affects:

| Written test case | Title |
|---|---|
| TC-201 | Statement PDF leaves out disputed payments |
| TC-202 | Download a statement |
| TC-203 | Account number with letters |
| TC-204 | Old statements are archived |

**Why this step is fast after the first time.** TIA remembers the facts of every file it has read, keyed by the exact content of the file. When a new version changes one file out of a thousand, it reads one file.

**In short**

- TIA reads each file as text and writes down facts: addresses, data shapes, functions, tests, outgoing calls and written test cases.
- It never runs your code.
- It remembers what it has read, so only changed files are read again.

## 4. Step two: joining the services together

**In this chapter:** how TIA works out that one service calls another, and how sure it is.

Facts from one repository are not enough. The important question is which call in one service reaches which address in another. TIA joins them into one map. (Developers call it a *graph*: points joined by lines.)

TIA found **7 calls** that go out of a program and linked **3** of them to the service they reach.

These are the three links it made:

| The call | is linked to | How sure | How it was found |
|---|---|---|---|
| the call on line 9 of client.py (statement-service) | the address GET /accounts/{}/payments (payments-service) | confirmed in the source code | the address setting names the service (strategy R2) |
| the call on line 13 of client.py (customer-portal) | the address POST /accounts/{}/payments (payments-service) | confirmed in the source code | the address setting names the service (strategy R2) |
| the call on line 9 of client.py (customer-portal) | the address GET /accounts/{}/payments (payments-service) | confirmed in the source code | the address setting names the service (strategy R2) |

Take the first row. The statement service's call on line 9 of `client.py` uses the setting `PAYMENTS_BASE_URL`. The settings file says that setting means the payments service. The payments service has exactly one address that matches `GET /accounts/{}/payments`. All three facts are in the code, so TIA marks the link **confirmed in the source code**.

**How sure is TIA?** Every link carries a label saying how it was established. "Confirmed in the source code" is the strongest. When TIA can only infer a link, for example because a path matches but the code does not say which service it is calling, it labels the link *inferred* and treats anything that depends on it as a lead to check, not a fact. When it cannot link a call at all, it says so.

Here are the four calls it did **not** link, and why:

| The call | Why it was not linked |
|---|---|
| the call on line 25 of web/src/app/payments/payments.component.ts (customer-portal) | base URL not present in call |
| the call on line 29 of web/src/app/payments/payments.component.ts (customer-portal) | base URL not present in call |
| the call on line 8 of web/src/StatementRequest.jsx (statement-service) | base URL not present in call |
| the call on line 8 of web/src/StatementRequest.jsx (statement-service) | base URL not present in call |

All four are web pages asking their own service for data, written without a server address because the browser fills it in. TIA does not guess where they go. It lists them, and the report says the connections may be incomplete. That honesty is deliberate: a missing link is shown as missing, never hidden.

**In short**

- TIA joins the facts from all repositories into one map of who calls whom.
- Each link carries a label saying how sure TIA is: confirmed, inferred, or not linked.
- Calls it cannot link are listed with the reason, never guessed.

## 5. Step three: what exactly changed

**In this chapter:** how TIA compares the version before the change with the version after it.

When a developer proposes a change, TIA reads the code **before** and **after**, and compares the two sets of facts. It does not compare text line by line; it compares meaning. Here is what it reported for three different changes to the payments service:

| The change | What TIA says changed | Kind of change |
|---|---|---|
| Delete the `disputed` field | the data shape Payment (payments-service) | schema_field_removed |
| List payments newest first | the function ordered (payments-service, app.py) | modified |
| Reword a comment | the function list_payments (payments-service, app.py) | modified |

Three changes, three different results:

- **Deleting `disputed`** changed a data shape that other services receive. TIA records it as "a field removed from a data shape", a change other programs can notice.
- **Listing payments newest first** changed the inside of one function, `ordered`. Nothing that other programs receive changed in shape, but the order they see did.
- **Rewording a comment** also touched a function's text. TIA sees that only the comment changed, not what the function does, and treats it accordingly (chapter 7 shows the result).

These changed items are the starting points for the next step.

**In short**

- TIA compares the facts before and after a change, not the raw text.
- It tells a change other services can see (a removed field) from an internal one (a function's logic) from a harmless one (a comment).
- The changed items are where the next step starts.

## 6. Step four: following the change

**In this chapter:** how TIA follows a change from where it happened to everything it reaches.

From each changed item, TIA walks the map: to whatever uses that item, then to whatever uses those, and so on. It stops after a fixed number of steps (eight by default), so a large landscape cannot make it run forever. If it stops early, the report says so.

Here is the route it found from the deleted field to the statement PDF, one step per line:

1. The walk starts at the changed data shape.
2. It moves to the address that sends that shape out.
3. It crosses into the statement service through the call that was linked in chapter 4.
4. It follows the statement service's own code up to its PDF address.

The real route TIA recorded:

1. the data shape Payment (payments-service)
2. the address GET /accounts/{}/payments (payments-service)
3. the call on line 9 of client.py (statement-service)
4. the function fetch_payments (statement-service, client.py)
5. the function statement_rows (statement-service, app.py)
6. the function statement_pdf (statement-service, app.py)
7. the address GET /statements/{}/pdf (statement-service)

Read it from top to bottom: the changed shape is sent by the payments address; the statement service's call on line 9 receives it; `fetch_payments` makes that call; `statement_rows` uses `fetch_payments` (that is the function with the "no disputed payments" rule); `statement_pdf` uses `statement_rows`; and `statement_pdf` answers the PDF address. So the monthly statement PDF depends on the deleted field.

TIA keeps this route for every result. When it tells you something is affected, it can always show you why, step by step, with the file and line of each step.

**In short**

- TIA walks the map from each changed item to everything that depends on it, up to a fixed limit.
- Every result comes with its route, so you can check the reasoning yourself.
- In our example, the route leads from the deleted field all the way to the statement PDF.

## 7. Step five: deciding how serious it is

**In this chapter:** how TIA grades each affected item, using fixed rules rather than opinion.

Each thing the walk reaches becomes a *finding*. TIA grades each finding as critical, high, medium or low, using a fixed table of rules. The grade depends on two things: **what kind of change** it is (does it break the other side, or only change its behaviour?) and **how sure** TIA is of the route (confirmed, or only inferred). A finding reached only through an inferred link is capped at medium, so TIA does not raise its loudest alarms on something it could not prove. (An organisation can change that cap in its settings; the finding then names the cap rule it used.)

Here are the findings for deleting `disputed`:

| # | Seriousness | Affected | What TIA says | Tests that touch it |
|---|---|---|---|---|
| F1 | high | customer-portal (a call) | customer-portal depends on the changed provider through a fully resolved HTTP call at customer-portal:client.py:9. Change: Payment.disputed removed. | test_portal_payments |
| F2 | high | statement-service (a call) | statement-service depends on the changed provider through a fully resolved HTTP call at statement-service:client.py:9. Change: Payment.disputed removed. | test_statement_pdf |
| F3 | high | customer-portal (an address) | customer-portal endpoint GET /portal/accounts/{}/payments depends on the changed component transitively (customer-portal:app.py:16). Change: Payment.disputed removed. | test_portal_payments |
| F4 | high | statement-service (an address) | statement-service endpoint GET /statements/{}/pdf depends on the changed component transitively (statement-service:app.py:18). Change: Payment.disputed removed. | test_statement_pdf |

All four are **high**: removing a field that other services read can break them, and every route is confirmed in the code. Each finding names the rule that produced its grade, so the grade can be checked and, if a team disagrees, the rule changed:

| Finding | Rule that set the seriousness |
|---|---|
| F1 | SEV-BREAKING-CONFIRMED |
| F2 | SEV-BREAKING-CONFIRMED |
| F3 | SEV-BREAKING-CONFIRMED |
| F4 | SEV-BREAKING-CONFIRMED |
| F5 (newest-first change) | SEV-LOCAL |

Now compare the three changes side by side:

| The change | Findings | High | Medium | Low | TIA's message |
|---|---|---|---|---|---|
| Delete the `disputed` field | 4 | 4 | 0 | 0 |  |
| List payments newest first | 5 | 0 | 4 | 1 |  |
| Reword a comment | 0 | 0 | 0 | 0 | Documentation-only change to the code (docstrings); no behaviour changed, so nothing needs retesting. |

- **Deleting the field:** four high findings, because other services can break.
- **Listing newest first:** four medium findings and one low. Nothing breaks, but both the statement and the portal will show payments in a different order, so they should be checked.
- **Rewording a comment:** nothing, with the message shown. TIA saw that only a comment changed.

This is the core of TIA's value: the same tool says "stop and check" for the first change, "worth a look" for the second, and "nothing to do" for the third, and shows its reasons each time.

**The same input always gives the same answer.** There is no AI in any of these steps. The same code, settings and evidence always produce the same report, and TIA can re-run an old analysis to prove it.

**In short**

- Each affected item becomes a finding, graded by a fixed table of rules.
- The grade depends on the kind of change and on how sure TIA is of the route.
- Deleting the field gives high findings; reordering gives medium; a comment gives none.

## 8. Step six: what to test

**In this chapter:** how TIA turns findings into a test plan a tester can use without reading code.

Findings speak about code. Testers think about screens, reports and test cases. So TIA also writes a **test scope**: the things a person can actually test, named in their terms where the team has given names (chapter 2's settings file gave the PDF its name).

| What to test | Address | Service | Effect | Existing tests |
|---|---|---|---|---|
| Payments screen | `GET /portal/accounts/{account_id}/payments` | customer-portal | breaking | test_portal_payments |
| Monthly statement PDF | `GET /statements/{account_id}/pdf` | statement-service | breaking | test_statement_pdf |
| (no name given) | `GET /accounts/{account_id}/payments` | payments-service | breaking | test_list_payments, test_unknown_account_is_404 |

The first two rows use the names from the settings files: "Payments screen" and "Monthly statement PDF". A tester knows exactly what to open. The third row has no name because nobody gave one; it is the payments service's own address.

**TIA also checks the business rules.** Remember line 10 of the statement service, which leaves out disputed payments. TIA noticed that the statement applies that rule, but the portal, which gets the very same data, does not:

> 1 consumer(s) of GET /accounts/{account_id}/payments (payments-service) keep only items where `not p['disputed']` (fields: disputed); 1 do not: GET /portal/accounts/{account_id}/payments (customer-portal). If the rule applies to every consumer, these show items the others exclude; if it does not, confirm the difference is intended.

|  | Address | Service | Where the rule is |
|---|---|---|---|
| applies the rule | `GET /statements/{account_id}/pdf` | statement-service | statement_rows in app.py line 10: `not p['disputed']` |
| does not | `GET /portal/accounts/{account_id}/payments` | customer-portal | nowhere |

TIA does not decide which is right. Maybe the screen should show disputed payments and the statement should not. Maybe it is a bug. It raises the question, with the exact file and line, so a person can decide. This is often the most useful thing in a report, because nobody was looking for it.

**Written test cases.** Because TIA read the testers' own test cases in chapter 3, it also says which of them to run again. You will see that list in the pull-request comment in chapter 11.

**In short**

- TIA turns findings into a test scope: screens, reports and addresses, with the existing tests for each.
- It points out business rules that one consumer applies and another does not.
- It also lists which written test cases to run again.

## 9. What TIA does not know

**In this chapter:** how TIA reports the limits of its own knowledge, and why this matters.

A report that only listed what it found would look complete even when it was not. So every TIA report ends with its **unknowns**: things it could not see that might change the answer. Here are the unknowns for our example:

| Area | What TIA says |
|---|---|
| unresolved_outbound_calls | 4 outbound call site(s)/contract interaction(s) could not be resolved to a provider. |
| event_consumers | Event consumers are analysed for Kafka (code and tia-services.yaml declarations) only; consumers over other messaging (AMQP, JMS, cloud queues) are not seen. |
| external_consumers | Consumers outside the 3 analysed repositories cannot be seen; no external consumer inventory is connected. |
| compatibility_not_evaluated | Compatibility was not evaluated for: headers, error response bodies. |
| ci_results_unavailable | No CI test results have been ingested for 3 repository(ies) with relevant tests: their tests are listed as associated, not as passed or failed. |
| coverage_unavailable | No coverage has been ingested: whether a test executes a component is unknown, not 'no'. |
| runtime_evidence_absent | No runtime evidence has been ingested: relationships come from source and contracts only, and a dependency not seen in them is not excluded. |
| deployment_evidence_absent | No deployment records have been ingested: the revision running in each environment is unknown. |
| build_evidence_absent | No build records have been ingested: build status and the artifacts each revision produced are unknown. |

In plain words, the most important ones say:

- Four calls could not be linked (the web pages from chapter 4), so some connections may be missing.
- Services outside these three repositories cannot be seen. If another team's program also reads payments, TIA does not know about it.
- No test results, test coverage, live traffic records or deployment records were given to TIA, so it cannot say whether tests passed or what runs in production.

The report's overall verdict reflects this. Our analysis finished with the status `complete_with_gaps`: the work is done, but the gaps above limit confidence. TIA never calls a report clean when it is not.

The unknowns also explain the comment-only change from chapter 7. Even with nothing to retest, TIA gave a reason:

> Documentation-only change to the code (docstrings); no behaviour changed, so nothing needs retesting.

**In short**

- Every report lists what TIA could not see, so a short report is never mistaken for a safe one.
- The status `complete_with_gaps` means the analysis finished but has known blind spots.
- Giving TIA more information (next chapter) removes some of those blind spots.

## 10. Adding information from your pipelines

**In this chapter:** how test results and other records from your build system make the report more precise.

TIA reads code, but code alone cannot tell whether a test passed last night or what version is running in production. Your build and release tools know those things. They can send that information to TIA: test results, test coverage, which version was deployed where, builds, and records of real traffic between services. TIA keeps those records and uses them in the next analysis. It never reaches out to fetch them during an analysis, so a report can always be re-created from exactly the information it used.

Here is the same analysis as before, run once more after the build system reported that the statement service's PDF test failed in build 42:

| Test | Result in CI | Build | Run of the analysed version? | States TIA records |
|---|---|---|---|---|
| `test_statement_pdf` | failed | build-42 | yes | exists, associated, failed |

TIA keeps these facts separate on purpose: the test **exists**, it is **associated** with the affected code, and it **failed** in a run of the version being analysed. It never merges them into a single "tested" tick that could mean any of them.

The unknowns change too:

|  | What the report says about CI results |
|---|---|
| Without CI results | No CI test results have been ingested for 3 repository(ies) with relevant tests: their tests are listed as associated, not as passed or failed. |
| With CI results for statement-service | No CI test results have been ingested for 2 repository(ies) with relevant tests: their tests are listed as associated, not as passed or failed. |

**Try it yourself.** A build system sends test results with one request to TIA, for example:

```
curl -X POST "http://127.0.0.1:8080/api/v1/evidence/test-results?repo=statement-service&revision=<commit>&build_id=build-42" --data-binary @junit.xml
```

The administrator guide lists the other kinds (coverage, deployments, builds, live traffic, incidents).

**In short**

- Build and release tools can send test results, coverage, deployments and more to TIA.
- TIA uses them in the next analysis and records exactly which ones it used.
- Test facts are kept separate: exists, associated, passed or failed, and whether that run was of this version.

## 11. Pull requests: from a proposed change to a comment

**In this chapter:** what happens when a developer opens a pull request, from start to finish.

A *pull request* is a developer saying "here is my change, please review it before it goes in". Code-hosting systems (GitHub, GitLab, Bitbucket, Azure DevOps) can notify TIA when one is opened or updated. That notification is called a *webhook*. What happens next:

1. **TIA checks the notification is genuine** (it carries a signature or a password agreed in advance with the code host) and finds the repository and the organisation it belongs to. It answers immediately and puts the work in a queue.
2. **A worker picks up the job.** Several workers can run at once; they never take the same job twice. If the developer pushes again before the job runs, the older job is dropped and only the newest version is analysed.
3. **The worker fetches the latest code**, then runs the steps from chapters 3 to 8.
4. **TIA writes one comment on the pull request** and updates that same comment on every new push, so the conversation is not flooded.

Here is the real comment TIA wrote for our change. (It normally also contains a small diagram of the services involved; that one block is left out here.)

```markdown
<!-- tia-analysis -->
### TIA impact analysis - `complete_with_gaps`

`payments-service` <commit> -> <commit> - change type: **api_change**

> **Warning:** the analysis ran to completion but has unknowns that limit confidence. This is not a clean impact assessment.

**4 finding(s), 4 material** - 4 high

**Regression-test 7 of your 9 test case(s) (1 not affected by this change; 1 could not be mapped - review by hand).**

| Test case | Why | Change effect |
|---|---|---|
| `TC-101` View payments | tests GET /portal/accounts/{account_id}/payments, which this change reaches (breaking) | BREAKING |
| `TC-102` Pay with missing amount | tests GET /portal/accounts/{account_id}/payments (on page /accounts/:id/payments), which this change reaches (breaking) | BREAKING |
| `TC-103` Pay in USD | tests GET /portal/accounts/{account_id}/payments (on page /accounts/:id/payments), which this change reaches (breaking) | BREAKING |
| `TC-104` Reference too long | tests GET /portal/accounts/{account_id}/payments (on page /accounts/:id/payments), which this change reaches (breaking) | BREAKING |
| `TC-201` Statement PDF leaves out disputed payments | tests GET /statements/{account_id}/pdf, which this change reaches (breaking) | BREAKING |
| `TC-202` Download a statement | tests GET /statements/{account_id}/pdf (on page /statements), which this change reaches (breaking) | BREAKING |
| `TC-203` Account number with letters | tests GET /statements/{account_id}/pdf (on page /statements), which this change reaches (breaking) | BREAKING |

Could not be mapped (review by hand): `TC-204` Old statements are archived

**What to test** (entry points this change reaches)

| Entry point | Service | Change effect | Tests found |
|---|---|---|---|
| Payments screen (`GET /portal/accounts/{account_id}/payments`) | customer-portal | BREAKING | `test_portal_payments` |
| Monthly statement PDF (`GET /statements/{account_id}/pdf`) | statement-service | BREAKING | `test_statement_pdf` |
| `GET /accounts/{account_id}/payments` | payments-service | BREAKING | `test_list_payments`, `test_unknown_account_is_404` |

- **Possible rule gap** - 1 consumer(s) of GET /accounts/{account_id}/payments (payments-service) keep only items where `not p['disputed']` (fields: disputed); 1 do not: GET /portal/accounts/{account_id}/payments (customer-portal). If the rule applies to every consumer, these show items the others exclude; if it does not, confirm the difference is intended.
- **Consistency check** - GET /portal/accounts/{account_id}/payments (customer-portal) and GET /statements/{account_id}/pdf (statement-service) all take data from GET /accounts/{account_id}/payments (payments-service): call them for the same input and check they show the same records, except where a documented rule says otherwise.


<details><summary>Recommended tests</summary>

- `test_portal_payments` (customer-portal) - associated with customer-portal (test-http-call) on the impact path
- `test_list_payments` (payments-service) - tests GET /accounts/{account_id}/payments (test-http-call)
- `test_unknown_account_is_404` (payments-service) - tests GET /accounts/{account_id}/payments (test-http-call)
- `test_statement_pdf` (statement-service) - associated with statement-service (test-http-call) on the impact path

_Tests not listed are not shown to be safe to skip: the list is based on the evidence available, and a test with no graph path to the change may still be affected._

</details>



**Architecture (evidence scope)**

[architecture diagram left out here]
_Diagram summarises evidence edges among services in this analysis; not a full landscape map._

| Severity | Component | Evidence | Why |
|---|---|---|---|
| HIGH | customer-portal | source_confirmed (R2) | customer-portal depends on the changed provider through a fully resolved HTTP call at customer-portal:client.py:9 |
| HIGH | statement-service | source_confirmed (R2) | statement-service depends on the changed provider through a fully resolved HTTP call at statement-service:client.py:9 |
| HIGH | customer-portal | source_confirmed (R2) | customer-portal endpoint GET /portal/accounts/{}/payments depends on the changed component transitively (customer-portal:app.py:16) |
| HIGH | statement-service | source_confirmed (R2) | statement-service endpoint GET /statements/{}/pdf depends on the changed component transitively (statement-service:app.py:18) |

<details><summary>Required verification</summary>

- **customer-portal**: Run the listed verification and confirm the consumer still compiles and passes contract checks.
- **customer-portal**: Run: test_portal_payments (tests/test_portal.py)
- **customer-portal**: Verify compatibility with the deployed consumer version
- **statement-service**: Run the listed verification and confirm the consumer still compiles and passes contract checks.
- **statement-service**: Run: test_statement_pdf (tests/test_statement.py)
- **statement-service**: Verify compatibility with the deployed consumer version

</details>

**Contract changes**
- BREAKING: Payment.disputed removed

**Coverage:** 3 repo(s) analysed; 3/7 outbound calls resolved (43%).

**Unknowns that affect confidence**
- 4 outbound call site(s)/contract interaction(s) could not be resolved to a provider.
- Event consumers are analysed for Kafka (code and tia-services.yaml declarations) only; consumers over other messaging (AMQP, JMS, cloud queues) are not seen.
- Consumers outside the 3 analysed repositories cannot be seen; no external consumer inventory is connected.

<sub>Advisory only - does not block merge. analysis `<analysis-id>` - snapshot `<snapshot-id>` - config `cfg-2;sev-1;inf-2`</sub>
```

Everything from the earlier chapters is in it: the four high findings, the test scope with the testers' names, the rule-gap question, the written test cases to run again, the tests to run, and the unknowns. The last line says it is **advisory**: it informs the review, it does not block the merge, unless the organisation chooses to make its release rules enforcing.

**Who sees what.** The comment is visible to everyone who can see the pull request. So TIA only names other repositories in it if everyone who can read this pull request can also read those repositories. Anything else is replaced by a count.

**In short**

- A code host notifies TIA of a pull request; TIA queues the work and answers at once.
- A worker runs the analysis on the newest version and writes one comment, updated on each push.
- The comment is advisory and only shows what its readers are allowed to see.

## 12. How TIA's own code is organised

**In this chapter:** the parts of TIA and how they fit, for people who will work on TIA itself.

TIA's code is in the folder `tia/`, split into ten layers. A layer may use the layers below it, never the ones above. This keeps the important parts honest: the part that analyses changes sits below the part that talks to GitHub, so it cannot reach out to the internet in the middle of an analysis. The rule is checked automatically every time the tests run.

| # | Layer | What it is responsible for | Files |
|---|---|---|---|
| 1 | Entry points | The processes and commands that start everything: the API server, the queue worker, the command line and the schema migration. | 4 |
| 2 | HTTP API, web UI and agent surfaces | Every way in: the REST API and web UI, sign-in, permissions on each request, the shareable report page, MCP for coding assistants and the Knowledge Fabric adapter. | 11 |
| 3 | Orchestration, ingestion and jobs | Turns a request into work: resolves revisions, registers and syncs repositories, queues jobs and runs an analysis end to end, the same way for the UI, the API and a pull-request webhook. | 8 |
| 4 | Source-control and change-management connectors | Talks to the outside on the platform's behalf: webhooks in, pull-request comments and check runs out, repository permissions mirrored from the host, ServiceNow change records, and the Neo4j mirror of the graph for Knowledge Fabric. | 11 |
| 5 | Impact engine and tester views | The deterministic core: diff the change, walk the graph within bounds, attach evidence, score with the rule table, and turn the result into the report and the tester views (test scope, regression test selection, configuration checks, coverage, automation inventory, tester pack, graph questions). | 19 |
| 6 | Graph and identity resolution | Connects facts from many repositories into one typed graph: who calls which endpoint, which code reads which table, with the resolution strategy and evidence on every edge, plus the use-case structure (screens, elements, locators, requests, fields, rules). | 9 |
| 7 | Extraction (language adapters) | Reads code as bytes and parses it into facts (endpoints, call sites, functions, schemas, tables, tests, UI elements). | 22 |
| 8 | Tenancy, identity and persistence | Everything stored, and who it belongs to: tenant-bound database transactions, the analysis and evidence store with its audit chain, tenants and their configuration, users, roles, secrets and customer keys, retention and backup. | 11 |
| 9 | Rules (deterministic, versioned) | The rule tables and their evaluation: severity, release policy, contract and event compatibility, team coordination. | 6 |
| 10 | Foundations | Shared building blocks with no business logic: the data model, read-only git access, secret scrubbing, safe outbound connections, air-gap guard, transport security, logging, metrics and tracing. | 13 |

How the chapters of this guide map onto the code:

| Chapter | What happens | Where in `tia/` |
|---|---|---|
| 3. Reading the code | Facts from each file | `extraction.py`, `extract_python.py`, `extract_java.py`, `extract_ui.py`, `lang/` |
| 4. Joining the services | The map of who calls whom | `resolve.py`, `graph.py` |
| 5. What changed | Before and after compared | `impact.py`, `contracts.py`, `testscope.py` |
| 6. Following the change | The walk through the map | `impact.py` |
| 7. How serious | The rule table | `severity.py`, `policy.py` |
| 8. What to test | Test scope and rule checks | `testscope.py`, `coverage.py`, `automation.py`, `testerpack.py` |
| 9. Unknowns | The limits of the report | `impact.py` |
| 10. Pipeline information | Stored records used in analysis | `evidence.py`, `enrich.py` |
| 11. Pull requests | Notification, queue, comment | `webhooks.py`, `jobs.py`, `worker.py`, `publish.py`, `github.py` |

**Try it yourself.** Every module, with its size and what it uses, is listed in [the architecture page](../docs/ARCHITECTURE-AS-BUILT.html), which is generated from the code.

**In short**

- TIA is split into ten layers; each may only use the ones below it.
- The analysis cannot contact outside systems, which keeps its results repeatable.
- Each chapter of this guide corresponds to a handful of files.

## 13. Where information is kept, and who can see it

**In this chapter:** how TIA stores its information and keeps each customer's information separate.

**Where.** Everything TIA records is kept in one PostgreSQL database: the facts read from each file, the maps, the reports, the records from your pipelines, the job queue, users and settings. The code itself is kept as copies of the repositories in one folder on disk. TIA reads those copies but never runs them.

**Reports are saved as they were.** Each report is stored with a list of exactly what it was based on: the commit of every repository, the settings, and the pipeline records. If the code changes later, the old report still says what was true then, and TIA can re-run it to show the same inputs give the same result.

**A tamper-evident log.** Important events (an analysis requested, a setting changed, a user added) are written to a log where each entry is sealed together with the one before it. Changing or deleting an old entry breaks the seal, and TIA can check the whole log on request.

**Keeping customers apart.** One TIA installation can serve many organisations; each is called a *tenant*. Every stored row carries its tenant's ID, and the database itself refuses to show one tenant's rows to another, even if TIA's own code had a mistake. A request with no tenant sees nothing at all.

**Who sees what inside one organisation.** People sign in through the organisation's own login system. Each person has a role (viewer, engineer, release manager, auditor, administrator) that decides what they may do. Which repositories they may read follows the code host's own permissions, copied regularly. A repository someone may not read simply does not exist for them: TIA answers "not found", and their reports leave out its findings.

**In short**

- Everything is in one PostgreSQL database; code is kept as read-only copies on disk.
- Reports keep a record of exactly what they used, and important events go into a sealed log.
- Tenants are kept apart by the database itself; people see only the repositories they may read.

## 14. Connecting other tools

**In this chapter:** how AI assistants and other systems can use what TIA knows.

TIA's results are not only for people. Other tools can ask TIA the same questions:

- **Coding assistants.** TIA offers a set of read-only questions in a standard format called MCP, which coding assistants understand: "what does this analysis say", "what should be tested", "who uses this address". They see only what the signed-in user may see.
- **Knowledge Fabric.** A separate company system that stores documents (an API register, test cases, requirements) can ask TIA for a finished report through the address `POST /query/tia`. TIA answers in the format that system already expects. That format was built from the system's written description; it has not yet been checked against the real system.
- **Neo4j.** TIA can copy its map into a Neo4j database used by Knowledge Fabric, and join its addresses and test cases to the documents stored there, using shared identifiers only (the same address, the same test case ID), never similar wording. TIA's own database stays the original; the Neo4j copy can be rebuilt at any time.
- **Scripts.** A Python library (`sdk/python`) and the web API let any script start analyses and read results.

**In short**

- Assistants, Knowledge Fabric and scripts can all ask TIA questions, under the same sign-in and permissions as people.
- TIA can copy its map into Neo4j and link it to documents on shared identifiers only.
- The Knowledge Fabric connection has not yet been tested against the real system.

## 15. Try it on your own computer

**In this chapter:** how to start TIA with the example from this guide and repeat what you have read.

You need Python and git installed, and the project folder `Project TIA`.

1. Open a terminal in the `Project TIA` folder.
2. Start TIA with the example from this guide:

```
python tools/run_dev.py --demo --landscape lanes
```

3. Open `http://127.0.0.1:8080` in a browser. The page has numbered sections; these are the ones this guide used:

| Section on the page | What you do there | Chapter |
|---|---|---|
| 1. Ingest repositories | See the three repositories TIA has read | 3 |
| 2. Code graph | See the map of who calls whom | 4 |
| 3. Analyse a change | Choose a repository and a change | 5 to 8 |
| 4. Change Impact Report | Read the findings, the test scope, the unknowns and the comment | 7 to 11 |
| 5. Tester views | Coverage of the test cases and the list of screens and fields | 8 |

4. In section 3, choose **Repository** `payments-service`, **Head** `contract` (the branch that deletes `disputed`) and **Base** `main` (the version before), then press **Run analysis**. You should see the four high findings from chapter 7.
5. Try **Head** `behaviour` (newest first) and **Head** `docstring` (the comment change) and compare the results with chapter 7.

The same analysis from a terminal:

```
curl -X POST http://127.0.0.1:8080/api/v1/impact-analyses -H "content-type: application/json" -d "{\"target\":\"payments-service\",\"head\":\"contract\",\"base\":\"main\"}"
```

To stop TIA, press Ctrl+C in the terminal.

**In short**

- `python tools/run_dev.py --demo --landscape lanes` starts TIA with this guide's example.
- Section 3 of the page runs an analysis; section 4 shows the report.
- Compare `contract`, `behaviour` and `docstring` to see the three outcomes from chapter 7.

## What TIA has not yet been proven on

A guide should be honest about limits. Everything in this guide runs and is checked by TIA's automated tests on this example and on larger test landscapes. These parts have only been tested against stand-ins for the real systems, not the real ones: GitHub App installations, GitLab, Bitbucket, Azure DevOps, ServiceNow, company login systems, cloud key stores and Knowledge Fabric. TIA has not yet been measured on a very large codebase of real code (around 20 million lines), and has not had an independent security test.

## Glossary

| Word | Meaning in this guide |
|---|---|
| Address | Something another program can ask a service for, such as `GET /accounts/{account_id}/payments`. Developers call it an endpoint. |
| Branch | A line of work in a repository; a change is usually made on its own branch. |
| Call | A place in one program's code where it asks another program for something. |
| Commit | One saved version of the code in a repository, with an ID. |
| Data shape | The structure of information a service sends or receives, such as a payment's four fields. Developers call it a schema. |
| Diff | The difference between two versions of a file: lines removed and added. |
| Finding | One thing TIA says a change affects, with a grade and the route that led to it. |
| Graph, map | All the facts joined together: points (services, addresses, functions, tests) and lines (who calls or uses whom). |
| Inferred | A link TIA believes but could not prove from the code. Findings that depend on it are treated as leads, not facts. |
| Pull request | A proposed change waiting for review before it is added to the main code. |
| Repository | A folder of code with its full history, kept by git. |
| Rule gap | A business rule that one service applies to some data and another service, using the same data, does not. |
| Service | A program that does one job and that other programs ask for information. |
| Severity, grade | How serious a finding is: critical, high, medium or low, set by fixed rules. |
| Tenant | One organisation using a shared TIA installation; its information is kept apart from all others. |
| Test scope | The screens, reports and addresses a tester should check for a change, with the existing tests for each. |
| Unknown | Something TIA could not see that might change the answer, listed in every report. |
| Webhook | A notification a code host sends to TIA when something happens, such as a pull request being opened. |

## Where to go next

- [User guide](../docs/USER-GUIDE.md): every part of the web page, in detail.
- [Administrator guide](../docs/ADMIN-GUIDE.md): installing, configuring and running TIA.
- [Architecture as built](../docs/ARCHITECTURE-AS-BUILT.html): every module, generated from the code.
- [Demo walkthrough](../docs/DEMO-WALKTHROUGH.html): a 100-minute technical demo script.
- [Client demo script](../docs/DEMO-SPEECH.html): a 40-minute client demo on the Northwind Pay showcase, with what to click and what to say.
- [Product requirements](../docs/PRD.md): what TIA is required to do.

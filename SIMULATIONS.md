# SIMULATIONS

Every scenario is re-runnable against a running stack (`make up`). Logs are in `evidence/`.

## Part A evidence at a glance

| What the brief asks | Command | Evidence |
|---|---|---|
| Patch against the upstream tag | `make patches` | `fineract-customization/patch/*.patch` |
| Custom image compiles | `make build-fineract` | `evidence/build-fineract-patched.log` (no cache: `Applied 2 patch(es)`, `BUILD SUCCESSFUL in 10m 26s`) |
| Java tests pass | `make test-java` | `evidence/test-java.log` (15 unit + 5 integration tests, all PASSED) |
| Clean `docker compose up` with the custom Fineract | `make reset && make up`, then `make evidence-compose-up` | `evidence/compose-up.log` |
| Behaviours 1 to 4 | `make demo-fineract-gl` | `evidence/demo-fineract-gl-sync.log` |
| Same account sent 4 times | `make simulate-gl-sync` | `evidence/simulate-gl-sync.log` |
| Retry when Fineract is down | manual steps below | `evidence/demo-gl-sync-failures.log` |

## S-A1: Fineract GL account sync, behaviours 1 to 4

- **Initial state:** stack from `make reset && make up`. Fineract has no GL accounts.
- **Request:** `POST /glaccounts/sync` straight to Fineract, with fresh references on each run: create, repeat, change the name, reuse the `glCode`, call as a user without `SYNC_GLACCOUNT`, 10 identical calls in parallel, and stock `POST /glaccounts` with an `externalReference`.
- **Expected:** 200 → same id → 409 → 409 → 403 (then 200 once granted) → ten 200s with one id → 400.
- **Actual:** exactly that. All 15 checks PASS (`evidence/demo-fineract-gl-sync.log`).
- **Fineract state:** one account per reference. The name is unchanged after the rejected update.
- **ERP / integration state:** not involved.
- **Explanation:** the first run of this demo found a bug. Under 10 parallel calls, 6 were answered 403 or 409 instead of the existing account. No duplicate was created, but the responses were wrong. Fixed in patch `0002`. See DECISIONS.md D-007.

## S-A2: Same ERP account sent 4 times

- **Initial state:** stack up, no ERP account `GL-1200`.
- **Request:** `POST mock-erp /api/accounts` 4 times in parallel, then `POST integration /api/gl-accounts/sync` 4 times in parallel with the same `external_reference`, then once more with it.
- **Response:** ERP: one 201 and three 200. Sync: one 201, the other three 202 (still running) or 200 (finished); the replay is 200 with the 201's body (D-019).
- **ERP state:** 1 account. **Fineract state:** 1 GL account with `externalReference=GL-1200`. **Integration state:** 1 `gl_account_sync` row, `SYNCED`.
- **Expected:** 1 / 1 / 1, `SYNCED`. **Actual:** PASS (`evidence/simulate-gl-sync.log`). Re-running changes nothing.
- **Explanation:** ERP unique constraints, then the sync table's primary key plus `SKIP LOCKED`, then Fineract's reference check.
- **Note:** since D-020 the ERP seeds `GL-1200` at startup, so on a fresh stack all four ERP calls return 200.

## S-A3: Fineract down during sync

- **Initial state:** `GL-1200` synced. A new ERP account `GL-4100` (INCOME). Since D-020 the ERP seeds `GL-4100` at startup, so a rerun needs another new account.
- **Request:** `docker compose stop fineract`, then `POST /api/gl-accounts/sync`. Then `docker compose start fineract`, wait until healthy, `POST /admin/recover`.
- **Response:** while Fineract is down, `GL-4100` is `SYNC_PENDING` with the connection error recorded. After recovery: `{"gl_accounts_processed": 1}`.
- **Fineract state:** `GL-4100` exists with type INCOME. **Integration state:** `GL-4100` is `SYNCED`.
- **Expected:** pending while down, synced after recovery. **Actual:** as expected (`evidence/demo-gl-sync-failures.log`).
- **Explanation:** a connection error is safe to retry, so the row stays pending with a backoff, and the recovery sweep picks it up.
- **Note:** this one was run by hand, not from a script.

## Part B evidence at a glance

| What the brief asks | Command | Evidence |
|---|---|---|
| Office, RWF, loan product with synced GL accounts, API user | `make reset && make up && make evidence-bootstrap` | `evidence/bootstrap-run1.log` |
| Bootstrap is get-or-create; second run changes nothing | same | `evidence/bootstrap-run2.log` |
| Product terms and mappings; API user least privilege | same, or `make verify-fineract-setup` | `evidence/verify-fineract-setup.log` |

## S-B1: Bootstrap twice on an empty Fineract

- **Initial state:** stack from `make reset && make up`. Fineract has only its stock Head Office, USD and roles. ERP is empty.
- **Request:** `scripts/bootstrap.py` twice.
- **Expected:** run 1 creates the office, adds RWF, creates the role, the user, 9 ERP accounts and the product, and every GL account is `SYNCED`. Run 2 prints `exists, unchanged` for every step. Both exit 0.
- **Actual:** pending a run on a fresh stack (`make evidence-bootstrap`). On a development stack that already had Part A data, run 1 created everything except `GL-1200` (already there from S-A2), and the next run reported every step unchanged.
- **Also checked by hand:** with the product's interest changed to 20, or one extra permission on the role, bootstrap stops and lists the difference (`expected 20, found 18.0`) without changing Fineract.

## S-B2: Least-privilege API user

- **Initial state:** after S-B1.
- **Request:** `scripts/verify_fineract_setup.py`, every call as `integration-api`.
- **Expected:** reads Kigali Main Office and the SAF product (RWF, 18% per year, 12 monthly, single disbursement, cash), and each of the 9 mappings points at the GL account with the expected ERP reference. Gets 403 on 6 admin endpoints.
- **Actual:** 18/18 PASS on the development stack. To be captured in `evidence/verify-fineract-setup.log` on a fresh stack.
- **Known gap (reported, not counted):** `PUT /currencies` is not authorized by Fineract 1.16.1, so the probe gets past security (404 for an unknown code). See DECISIONS.md D-013.

## Loan lifecycle evidence at a glance

| What the brief asks | Command | Evidence |
|---|---|---|
| Loan from application to payoff, idempotent, one disbursement | `make simulate-loan-lifecycle` | `evidence/simulate-loan-lifecycle.log` (41/41 checks PASS) |
| One ERP journal entry per Fineract transaction, with Fineract's split | same | same, sections 4 to 9 |

## S-D1: Loan lifecycle with ERP posting

- **Initial state:** stack from `make reset`, then `make bootstrap`; the ERP has its 9 seeded accounts. Every run uses new references and checks balance movements rather than absolute balances, so it can be re-run. The logged run is the second on that stack, so its ERP ids start at `JE-000006`.
- **Request:** onboard a client; apply, repeat, and reuse the reference with another principal; approve; 10 parallel disbursements with different references; repayment 1 sent 4 times; repayments 2 and 3; a payoff of Fineract's `totalOutstanding` (plus a reused reference and an overpayment, both refused).
- **Expected:** one 201 and nine 409 for the disbursement; 201 then 200 ×3 for the repeated repayment; loan "Closed (obligations met)". In the ERP: one entry per disbursement and repayment, with lines equal to Fineract's transaction (Dr 1200 / Cr 1100; Dr 1100 / Cr 1200 principal / Cr 4100 interest), none for the rejected disbursements, and balances moved by exactly Fineract's amounts.
- **Actual:** 41/41 checks PASS. Example from the log: repayment 1 of 500,000 RWF split by Fineract into 383,400 principal and 116,600 interest, posted as `JE-000007` with exactly those lines. Over the loan, 1100 moved by +500,799, 1200 by 0 and 4100 by +500,799: the interest Fineract charged.
- **Fineract state:** one disbursement and four repayments, each with our `external_reference` as `externalId`.
- **ERP state:** five entries, keyed by the same references. **Integration state:** every money operation `COMPLETED` with an `erp_transaction_id`.
- **Explanation:** the journal entry is built from Fineract's transaction and saved before the ERP call (D-022), so repeats post nothing new. The ERP's unique reference is the guard behind that.

## Part G evidence at a glance

| What the brief asks | Command | Evidence |
|---|---|---|
| Seeded chart, idempotent journal entries, unbalanced entries rejected, balances | `make simulate-erp` | `evidence/simulate-erp.log` (not yet captured) |
| Journal rules (balance, line shape, currency, balance sign) | `make test-python` | 16 mock-erp unit tests |

## S-G1: Mock ERP journal entries

- **Initial state:** stack up; the ERP has seeded its 9 accounts.
- **Request:** the same balanced entry (Dr 1300 / Cr 1100, 250,000 RWF) 10 times in parallel; read it back; same reference with a different amount; four invalid entries (unbalanced, unknown account, one line, debit and credit on one line); an offsetting entry; the balance of an unknown account.
- **Expected:** one 201 and nine 200s with the same body; 1300 up and 1100 down by 250,000 exactly once; 409; each invalid entry 422 and a GET for it 404; balances back to their starting values; 404.
- **Actual:** not yet captured on the compose stack. To be recorded in `evidence/simulate-erp.log`.
- **ERP state:** two `SIM-ERP-` entries per run; balances unchanged overall. **Fineract / integration state:** not involved.
- **Explanation:** the unique constraint on `journal_entries.external_reference` decides which concurrent request inserts; the others read the stored entry and compare the request hash. Invalid entries fail validation before anything is written.

## Part I evidence at a glance

All four scenarios run with `make simulate-concurrency` against the running stack. Evidence: `evidence/simulate-concurrency.log`, **35/35 checks PASS**. Every parallel batch is sent by a real thread pool, and each thread waits on a barrier so all the requests leave at once. Counts come straight from each system: Fineract's loan transactions, `count(*)` on the ERP's `journal_entries`, and `count(*)` by status on the integration service's `operations`. How the layers work together: DECISIONS.md D-026.

| # | Scenario | Loan | Log lines |
|---|---|---|---|
| S-I1 | Same disbursement sent 4 times in a row | `LN-2026-000013` | 16–30 |
| S-I2a | 10 simultaneous disbursements, same reference | `LN-2026-000014` | 32–47 |
| S-I2b | 10 simultaneous disbursements, different references | `LN-2026-000015` | 49–64 |
| S-I3 | 10 repayment references × 3 copies, 30 at once | `LN-2026-000013` | 66–105 |

## S-I1: Sequential duplicate disbursement

- **Initial state:** approved loan `LN-2026-000013`, nothing disbursed.
- **Request:** `POST /api/loans/LN-2026-000013/disbursement` with `SIMC-443936-I1-DISBURSE`, 4 times in a row.
- **Response:** `[201, 200, 200, 200]`; the three replays return the first body exactly.
- **Fineract state:** one disbursement, `externalId` `SIMC-443936-I1-DISBURSE`; status Active, outstanding 5,500,799.
- **ERP state:** 1 journal entry. **Integration state:** 1 operations row, `COMPLETED`.
- **Expected:** requests 4, Fineract disbursements 1, ERP entries 1, integration transactions 1.
- **Actual:** exactly that (lines 22–26).
- **Explanation:** the second request hits the unique `(operation_type, external_reference)` constraint, finds a finished operation with the same payload hash, and replays the stored response. It never reaches Fineract or the ERP.

## S-I2a: Concurrent disbursements, same reference

- **Initial state:** approved loan `LN-2026-000014`.
- **Request:** 10 simultaneous `POST .../disbursement` with `SIMC-443936-I2A-DISBURSE`.
- **Response:** `[201, 202 ×9]`. One request inserts the operation and runs it; the other nine find it in progress and get 202 with its current status. The same request afterwards gets 200 with the original result.
- **Fineract state:** one disbursement. **ERP state:** 1 entry. **Integration state:** 1 operations row, `COMPLETED`.
- **Expected:** exactly one disbursement in every system; no request rejected.
- **Actual:** exactly that (lines 33–47).
- **Explanation:** the unique constraint lets only one `INSERT` succeed. The losers read the existing row instead of executing.

## S-I2b: Concurrent disbursements, different references

- **Initial state:** approved loan `LN-2026-000015`.
- **Request:** 10 simultaneous `POST .../disbursement`, references `SIMC-443936-I2B-DISBURSE-0` to `-9`.
- **Response:** `[201, 409 ×9]`. Each 409 says the loan "is already disbursed or being disbursed by another request".
- **Fineract state:** one disbursement, carrying the winner's reference (`-2`). **ERP state:** 1 entry. **Integration state:** 10 operations rows: 1 `COMPLETED`, 9 `FAILED`. The nine never reached Fineract.
- **Expected:** exactly one disbursement in Fineract and in the ERP.
- **Actual:** exactly that (lines 50–64).
- **Explanation:** the references differ, so the idempotency constraint lets all ten in. Next, each request locks the `loan_map` row (`SELECT ... FOR UPDATE`) and tries to claim the loan's single `loan_disbursement` slot (primary key `loan_reference`). One claims it; the other nine see a slot owned by a live operation and are rejected before any Fineract call. If those two guards failed, Fineract's loan state would still refuse a second disbursement of an Active loan (D-026).

## S-I3: Concurrent repayments with duplicates

- **Initial state:** loan `LN-2026-000013` (disbursed in S-I1), Fineract total outstanding 5,500,799 (principal 5,000,000, interest 500,799).
- **Request:** 10 references `SIMC-443936-I3-REPAY-0` to `-9`, 100,000 RWF each on 2026-05-01. Each is sent 3 times, and all 30 requests leave together.
- **Response:** `[201 ×10, 202 ×20]`: one 201 per reference; the 20 duplicates found their reference in progress. Replaying each reference afterwards returns 200 `COMPLETED`.
- **Fineract state:** exactly one repayment per reference, 10 in total. Fineract split them unevenly (e.g. `REPAY-9` 25,000 principal / 75,000 interest, `REPAY-1` 100,000 / 0) because it allocates each one against the schedule in the order it booked them. Fineract recorded 1,000,000 in total: 792,339 principal and 207,661 interest.
- **Balances:** total 5,500,799 − 1,000,000 = 4,500,799; principal 5,000,000 − 792,339 = 4,207,661; interest 500,799 − 207,661 = 293,138. Each matches Fineract's own summary (lines 89–91). The subtractions use Fineract's recorded portions, not the amounts the test sent.
- **ERP state:** 10 entries, each with Fineract's split. **Integration state:** 10 operations rows, all `COMPLETED`.
- **Expected:** each reference applied once; the duplicates applied nothing; balances moved by exactly Fineract's breakdown.
- **Actual:** exactly that. During this run Fineract answered 2 of the repayment calls with a concurrency 409 (it had rolled each back); the service resent them and each landed once (line 101).
- **Explanation:** duplicates are stopped by the idempotency constraint, as in S-I2a. Different references are meant to all apply, so there is no per-loan slot. Concurrent writes to one loan are serialised by Fineract's optimistic locking. When Fineract's own retries run out it returns 409 `error.msg.platform.service.*.conflict`, and the service now treats that as retryable rather than as a rejection (D-026). The first run of this simulation, before that fix, failed: 6 of the 10 references ended `FAILED` with 422. Fineract and the ERP still agreed on the 4 that applied.

## Part J evidence at a glance

All five scenarios run on one loan with `make simulate-failures` (needs `FAULT_INJECTION_ENABLED=true` in `.env`). Evidence: `evidence/simulate-failures.log`, **52/52 checks PASS**. For every scenario the log prints our operation (`GET /admin/operations/{ref}`), Fineract's transactions with that `externalId`, and the ERP entry plus a `count(*)` straight from the ERP database, before, during and after recovery. How faults are injected: DECISIONS.md D-023.

| # | Scenario | How the failure is produced | Log lines |
|---|---|---|---|
| S-J1 | ERP down after Fineract success | `docker compose stop mock-erp` (real outage) | 9–40 |
| S-J2 | Fineract down | `FINERACT_DOWN` fault, before any Fineract call | 42–71 |
| S-J3 | Fineract timeout after sending | `FINERACT_TIMEOUT_AFTER_SEND` fault, after Fineract booked it | 73–104 |
| S-J4 | ERP timeout after the entry was written | `ERP_TIMEOUT_AFTER_WRITE` fault, after the ERP stored it | 106–140 |
| S-J5 | Crash between Fineract success and the ERP call | `CRASH_BEFORE_ERP` fault: `os._exit(1)`, Docker restarts the container | 142–168 |

## S-J1: ERP down after Fineract success

- **Initial state:** approved loan `LN-2026-000005`; no operation, Fineract transaction or ERP entry for `SIMF-432922-S1-DISBURSE`.
- **Request:** stop `mock-erp`, then `POST /api/loans/{ref}/disbursement`.
- **During:** the caller gets **202** with `operation_status: ERP_PENDING` and Fineract's result (5,000,000 disbursed). The operation is `ERP_PENDING`, attempts 1, `last_error` the connection error. Fineract has one disbursement; the ERP database has 0 rows. The same request again gets 202 and Fineract still has one disbursement. `POST /admin/recover` while the ERP is down leaves it `ERP_PENDING`.
- **After:** start `mock-erp`; `POST /admin/recover` finishes it in 4 s. `COMPLETED`, `JE-000017` = Dr 1200 / Cr 1100 5,000,000, one row; 1200 moved by 5,000,000 once. The same request again gets 200 with `JE-000017` and still one row.
- **The brief's questions.** *Status?* `ERP_PENDING`, reported to the caller as 202. *Retried?* Yes: 3 inline attempts, then deferred retries with backoff (5 s, 10 s, … 5 min) from the sweep, up to 10; then `NEEDS_RECONCILIATION`, never `FAILED` (not exercised by this run; unit test `test_erp_attempts_start_from_zero_and_end_in_reconciliation`). *How does the ERP catch up?* The entry was saved at `FINERACT_DONE` (D-022); the sweep looks it up by reference, then posts exactly those bytes. *Double posting?* The lookup first; the saved request makes any resend identical; the ERP's unique `external_reference` stores it once.

## S-J2: Fineract down

- **Initial state:** no operation, Fineract transaction or ERP entry for `SIMF-432922-S2-REPAY`.
- **Request:** arm `FINERACT_DOWN`, then `POST /api/loans/{ref}/repayments` for 500,000.
- **During:** the caller gets **202** with `operation_status: RECEIVED`. Persisted: the operation row (payload, hash, `RECEIVED`, attempts 1, `last_error` "injected fault: Fineract is down"). Nothing in Fineract, nothing in the ERP. The same request again gets 202 and nothing executes; the same reference with another amount gets 409. A recovery pass 6 s later retries (attempts 2) and still finds Fineract down.
- **After:** clear the fault; recovery finishes it in 11 s. `COMPLETED`; one Fineract repayment (383,400 principal, 116,600 interest); `JE-000018` with those lines, one row.
- **The brief's questions.** *What does the caller get?* 202 and the operation status, not an error: the request is accepted and will be retried. *Is anything persisted?* Only our operation row; nothing reached Fineract or the ERP. *Safe to retry?* Yes, with the same `external_reference` and payload: it replays (202 now, the stored result later) and never executes twice. If Fineract stays down for all 10 attempts, the operation ends `FAILED` with 503, which means nothing happened (not exercised by this run; unit test `test_fineract_down_on_every_attempt_fails_with_503_and_nothing_sent`).

## S-J3: Fineract timeout after sending

- **Initial state:** no operation, Fineract transaction or ERP entry for `SIMF-432922-S3-REPAY`.
- **Request:** arm `FINERACT_TIMEOUT_AFTER_SEND`, then the repayment.
- **During** (the service log of this request, by correlation id): one `POST .../transactions?command=repayment`, then `Fault fired` and `Fineract attempt failed`, then `GET .../transactions/external-id/SIMF-432922-S3-REPAY` returning 200, and no second POST. (The GET before the fault is `execute` reading back the transaction it created; the fault throws that answer away.)
- **After:** the caller gets **201** with Fineract's split. One Fineract repayment; `JE-000019`, one row.
- **The brief's question.** *How do you know whether it succeeded?* Our `external_reference` is sent as the transaction's `externalId`. After a timeout the pipeline never resends: it looks the transaction up by that id (`/loans/external-id/{loan}/transactions/external-id/{ref}`). Found: done. Not found: safe to send. Lookup fails too: retry later, and after the last attempt `NEEDS_RECONCILIATION` (unit test `test_last_attempt_with_unknown_outcome_needs_reconciliation`; this run shows the found case). Fineract's unique index on `externalId` is the guard behind it.

## S-J4: ERP timeout after the entry was written

- **Initial state:** no operation, Fineract transaction or ERP entry for `SIMF-432922-S4-REPAY`.
- **Request:** arm `ERP_TIMEOUT_AFTER_WRITE`, then the repayment.
- **During** (service log): `POST .../api/journal-entries` returning 201 Created, then `Fault fired` and `ERP attempt failed`, then `GET .../api/journal-entries/SIMF-432922-S4-REPAY` returning 200. No second POST.
- **After:** **201**; `JE-000020`; ERP row count **1**; 1100 moved by 500,000 once. A blind resend of the same entry straight to the ERP gets 200 with `JE-000020`. The partner repeating the request gets 200 with `JE-000020`. Still one row.
- **The brief's questions.** *What happens on retry?* The next attempt looks the entry up by reference and finds it, so nothing is posted again. Even a resend would be byte-identical and get the stored entry back. *Prove one journal entry exists:* `count(*) = 1` from the ERP database, and the balance moved once. (The blind resend's insert hit the unique constraint and rolled back. That still used a database sequence number, which is why the next entry in S-J5 is `JE-000022`, not `JE-000021`.)

## S-J5: Crash between Fineract success and the ERP call

- **Initial state:** no operation, Fineract transaction or ERP entry for `SIMF-432922-S5-REPAY`; container restart count 1 (from the previous run).
- **Request:** arm `CRASH_BEFORE_ERP`, then the repayment. The process exits after `ERP_PENDING` is saved and before the ERP is called.
- **During:** the caller's connection drops (`RemoteDisconnected`). Docker restarts the container (restart count 1 → 2) and it is healthy 2 s later. Right after the restart: operation `ERP_PENDING`, Fineract has the repayment (448,527 principal, 51,473 interest), the ERP has 0 rows.
- **After:** with no manual action, the in-process recovery sweep completes it 59 s later, once the request's 60 s lease has expired: `COMPLETED`, `JE-000022` with Fineract's lines, one row.
- **The brief's question.** *How is it recovered on restart?* State is saved before each call to the next system (rule 8), so the restart finds the operation in `ERP_PENDING` with its journal entry already saved. The recovery sweep runs at startup and every 30 s, and claims due rows whose lease has expired (`FOR UPDATE SKIP LOCKED`). It looks the entry up by reference (not found here), posts it, and completes the operation. A crash one step earlier, between Fineract and `FINERACT_DONE`, leaves `FINERACT_PENDING`; recovery then looks the Fineract transaction up by `externalId` before doing anything (D-014). Both crash points are also unit-tested (`tests/test_pipeline.py`).

## Part K evidence at a glance

`make simulate-reconciliation` seeds all four outcomes and one reversal on one new loan. It reports by references and by date range, then repairs what may be repaired. Evidence: `evidence/simulate-reconciliation.log`, **29/29 checks PASS**. Design: DECISIONS.md D-024.

| # | What | Log lines |
|---|---|---|
| S-K1 | Seed all four outcomes and a reversal | 3–30 |
| S-K2 | Report by references, by date range, and read-only | 31–68 |
| S-K3 | Explicit, idempotent repair | 69–106 |

## S-K1: Seeding the four outcomes

- **Initial state:** a new client and loan `LN-2026-000006`, disbursed through the integration API on 2026-07-25. Each run uses its own three-day window (here 2026-07-25 to 2026-07-27), so the date-range report holds only this run's rows.
- **Seeds:**

| Reference | How | Expected |
|---|---|---|
| `SIMK-438778-DISBURSE`, `-REPAY-1` | Integration API | `MATCHED` |
| `SIMK-438778-REPAY-UNDONE` | Integration API (ERP entry `JE-000025`), then reversed in Fineract (`POST /loans/external-id/{loan}/transactions/external-id/{ref}` with `transactionAmount: 0`) | Original `MATCHED`; `-REV` `MISSING_IN_ERP` |
| `SIMK-438778-BRANCH-1` | Repayment of 300,000 booked straight in Fineract (back-office user) | `MISSING_IN_ERP` |
| `SIMK-438778-BRANCH-2` | Repayment of 250,000 straight in Fineract; the ERP entry was keyed by hand with principal 161,588 instead of Fineract's 186,588 | `AMOUNT_MISMATCH` |
| `SIMK-438778-ERP-ONLY` | A manual ERP entry of 150,000 (Dr 1100 / Cr 1200) with no Fineract transaction | `MISSING_IN_FINERACT` |

- **Fineract state:** after the undo, `REPAY-UNDONE` has `manuallyReversed: true` and `reversedOnDate` 2026-10-31 (the day it was undone).

## S-K2: The report

- **Request:** `POST /api/reconciliation` with `external_references` (the seven references above, one of them unknown to both systems), then with `from_date`/`to_date` for the run's window.
- **Response (both modes):** summary `MATCHED 3, MISSING_IN_ERP 2, MISSING_IN_FINERACT 1, AMOUNT_MISMATCH 1`. The mismatch lists `amount: Fineract 250000, ERP 225000`, `1100 debit: …` and `1200 credit: Fineract 186588, ERP 161588`. The unknown reference is in `not_found`. Each row carries the `loan_reference`, our operation status when one exists (`-` for the transactions made outside the integration), Fineract's side and the ERP's side. No Fineract numeric ids appear.
- **Read-only:** across the three report calls, the ERP row count stayed at 25 and the loan's Fineract transactions were unchanged.
- **Expected = actual:** every seeded reference got its expected status, the same in both modes.

## S-K3: Repair

- **Request:** `POST /api/reconciliation/repairs` with `{external_reference, loan_reference, transaction_reference}`.
- **`MISSING_IN_ERP` (BRANCH-1):** 201 with `JE-000028`, built from Fineract's split (272,351 principal, 27,649 interest). The same request again gets 200 with `JE-000028`. A second repair reference for the same transaction gets 201 with the same `JE-000028`. The ERP holds one row. The same repair reference with a different payload gets 409.
- **Missing reversal (`REPAY-UNDONE-REV`):** 201 with `JE-000030`, the mirror of `JE-000025`.
- **Refused:**
  - `AMOUNT_MISMATCH` gets 409 ("the ERP already holds a different journal entry"); the entry is untouched.
  - `MISSING_IN_FINERACT` gets 404; nothing is written to Fineract.
  - A `-REV` for a transaction that isn't reversed gets 409.
- **After:** the date-range report shows `MATCHED 5, MISSING_IN_FINERACT 1, AMOUNT_MISMATCH 1`. The two rows a person must resolve are still there.
- **Explanation:** the report reads Fineract live and builds the expected entry with the same mapping the posting step uses (D-021), so it compares against Fineract's numbers. Repair only adds what is missing in the ERP. It goes through the operations pipeline, so it is idempotent on its own reference, and the ERP's unique `external_reference` makes it idempotent per transaction as well.

## Part L evidence at a glance

`make simulate-gl-reconciliation` seeds one GL account for each master-data outcome with codes no other run uses. It reports twice, runs the GL sync, then reports again. Evidence: `evidence/simulate-gl-reconciliation.log`, **23/23 checks PASS**. Design: DECISIONS.md D-025.

| # | What | Log lines |
|---|---|---|
| S-L1 | Seed every outcome | 1–11 |
| S-L2 | Report, twice, read-only | 12–44 |
| S-L3 | GL sync, then report again | 45–65 |

## S-L1: Seeding the outcomes

- **Initial state:** the bootstrapped stack. Every code and reference carries the run number (here `441295`, prefix `SIML-441295`).
- **Seeds:**

| Reference / code | How | Expected |
|---|---|---|
| `SIML-441295-MATCHED` / `94412950` | ERP account, synced through `POST /api/gl-accounts/sync` | `MATCHED` |
| `SIML-441295-RENAMED` / `94412951` | ERP account, synced, then renamed with Fineract's stock `PUT /glaccounts/{id}` | `DETAILS_MISMATCH` |
| `SIML-441295-NOT-SYNCED` / `94412952` | ERP account created after the sync | `MISSING_IN_FINERACT` |
| `94412953` | Fineract's stock `POST /glaccounts`, no external reference | `MISSING_IN_ERP` |
| `SIML-441295-ORPHAN` / `94412954` | Fineract `POST /glaccounts/sync` with a reference the ERP never had | `MISSING_IN_ERP` |
| `SIML-441295-CLASH` / `94412955` | Fineract's stock `POST /glaccounts`, then an ERP account with the same code | `CODE_CONFLICT` |

- The direct Fineract calls use the admin user and stand in for a back-office user working outside the integration.

## S-L2: The report

- **Request:** `GET /api/reconciliation/gl-accounts`, twice.
- **Response:** every seeded account got its expected status.
  - `RENAMED` lists `name: ERP 'Sim Renamed 441295', Fineract 'Sim Renamed 441295 (edited)'`.
  - `CLASH` is one row carrying both sides, with the note that sync never adopts an existing account.
  - The two `MISSING_IN_ERP` rows say why: no external reference, or a reference the ERP does not hold.
  - `NOT-SYNCED` has `sync_status: null`.
  - No Fineract numeric ids appear.
- **Read-only:** Fineract GL accounts and ERP accounts were 14 and 13 before and after both calls. Both calls returned the same rows.

## S-L3: Sync, then report again

- **Request:** `POST /api/gl-accounts/sync`, then the report.
- **Result:**
  - `NOT-SYNCED` is now `MATCHED` (`sync_status: SYNCED`).
  - `CLASH` stays `CODE_CONFLICT` with `sync_status: FAILED`, because Fineract refused it with `error.msg.glaccount.glcode.conflict`. The existing account was not adopted.
  - `RENAMED` still shows the edited name. Sync is create-only and did not overwrite it.
  - The other rows are unchanged.
- **Explanation:** the report reads both systems live and compares the fields sync writes. It changes nothing. The only automatic fix is the normal sync, which closes `MISSING_IN_FINERACT` gaps. The other statuses need a person (D-025).


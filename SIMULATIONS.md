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

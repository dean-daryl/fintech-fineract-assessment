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

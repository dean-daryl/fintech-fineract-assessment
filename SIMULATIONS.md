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
- **Request:** `POST mock-erp /api/accounts` 4 times in parallel, then `POST integration /gl-accounts/sync` 4 times in parallel.
- **Response:** ERP: one 201 and three 200. Sync: four 200s. One call shows `SYNCED`, and the others show `SYNC_PENDING` because they found the account locked by the first call and skipped it.
- **ERP state:** 1 account. **Fineract state:** 1 GL account with `externalReference=GL-1200`. **Integration state:** 1 `gl_account_sync` row, `SYNCED`.
- **Expected:** 1 / 1 / 1, `SYNCED`. **Actual:** PASS (`evidence/simulate-gl-sync.log`). Re-running changes nothing.
- **Explanation:** ERP unique constraints, then the sync table's primary key plus `SKIP LOCKED`, then Fineract's reference check.

## S-A3: Fineract down during sync

- **Initial state:** `GL-1200` synced. A new ERP account `GL-4100` (INCOME).
- **Request:** `docker compose stop fineract`, then `POST /gl-accounts/sync`. Then `docker compose start fineract`, wait until healthy, `POST /admin/recover`.
- **Response:** while Fineract is down, `GL-4100` is `SYNC_PENDING` with the connection error recorded. After recovery: `{"gl_accounts_processed": 1}`.
- **Fineract state:** `GL-4100` exists with type INCOME. **Integration state:** `GL-4100` is `SYNCED`.
- **Expected:** pending while down, synced after recovery. **Actual:** as expected (`evidence/demo-gl-sync-failures.log`).
- **Explanation:** a connection error is safe to retry, so the row stays pending with a backoff, and the recovery sweep picks it up.
- **Note:** this one was run by hand, not from a script.

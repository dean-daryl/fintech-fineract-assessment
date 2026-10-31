# ARCHITECTURE

Three services and one PostgreSQL server, started together by Docker Compose. Decisions are referenced by their number in [DECISIONS.md](DECISIONS.md); the evidence for each behaviour is in [SIMULATIONS.md](SIMULATIONS.md).

## Components

| Component | Language | Owns | Port (host) |
|---|---|---|---|
| **Fineract** (custom image, tag `1.16.1` + our patch) | Java / Spring Boot | Clients, loans, loan status, balances, the principal/interest split. Source of truth for loans. Its GL accounts mirror the ERP's | 8443 (HTTPS) |
| **Mock ERP** | Python / FastAPI | The chart of accounts and journal entries. Rejects unbalanced entries; enforces its own uniqueness | 8001 |
| **Integration Service** | Python / FastAPI | The only partner-facing API. Idempotency, the operation state machine, GL sync, ERP-to-Fineract type mapping, accounting mapping, retries, recovery, reconciliation | 8000 |
| **PostgreSQL 18.3** | | One server, one database and login per service: `fineract_default`, `erp`, `integration` (D-003) | 5433 |

Who calls whom:

```
                 X-API-Key
  Partner  ───────────────────►  Integration Service  ──────► Fineract  (REST, least-privilege user)
                                     │       ▲
                                     │       └── reads ERP accounts (GL sync)
                                     └─────────────► Mock ERP  (journal entries)
```

Fineract and the ERP never call each other or the integration service. Every cross-system effect starts in the integration service, which is what lets it own idempotency and recovery in one place. No message broker or worker: retries and recovery run from the integration service's own operations table (D-014, D-023).

## Fineract customization

A patch on tag `1.16.1` (`fineract-customization/patch/`), applied while the image builds from source (D-002, D-005). It adds, inside Fineract (D-007):

- `acc_gl_account.external_reference`, unique, through Liquibase changeset `0270`.
- `POST /glaccounts/sync`, a new command in Fineract's own command pipeline, so permissions, audit and the transaction boundary are the stock ones. It is protected by a new permission, `SYNC_GLACCOUNT`.
  - Same reference and same details: returns the existing account.
  - Same reference and different details: 409.
  - A `glCode` taken by another account: 409.
  - Concurrent identical calls: one account. The unique constraint plus Fineract's command retry decide the race.
- `GET /glaccounts/external-reference/{ref}`, so the integration service can look an account up before retrying.
- JUnit tests: 15 unit tests and 5 integration tests against the running image.

## Data flows

### GL accounts: ERP → Fineract (Part A)

1. The ERP seeds its chart of accounts at startup (D-020). New accounts come through `POST /api/accounts` on the ERP.
2. A partner or bootstrap calls `POST /api/gl-accounts/sync` with an `external_reference` (D-008, D-019). The trigger is an explicit call, not a webhook or polling.
3. The integration service reads every ERP account and registers each one in `gl_account_sync` as `SYNC_PENDING`.
4. Each account is mapped to Fineract's GL type by one pure function (`domain/gl_mapping.py`: ASSET→1 … EXPENSE→5) and sent to `POST /glaccounts/sync`.
5. The account becomes `SYNCED`, or `FAILED` (rejected, or after 10 attempts), or stays `SYNC_PENDING` with a backoff while Fineract is unreachable.

Bootstrap runs this sync before it creates the loan product, so the product's GL mappings always point at synced accounts (D-009, D-011).

### A money operation: partner → Fineract → ERP (Parts D, F, H)

A repayment, step by step. Disbursement is the same, with the extra per-loan guard of D-026.

```
POST /api/loans/{loan_reference}/repayments  {external_reference, amount, transaction_date}
  │
  ├─ INSERT operations (operation_type, external_reference) ON CONFLICT DO NOTHING
  │     lost the insert → replay: 200 stored result / 202 in progress / 409 other payload
  │
  ├─ RECEIVED          prepare: loan exists and is active, amount ≤ Fineract's live outstanding
  ├─ FINERACT_PENDING  POST Fineract repayment with externalId = external_reference
  │                    (timeout → look it up by externalId; never resend blindly)
  ├─ FINERACT_DONE     read Fineract's transaction: amount, principal, interest
  │                    build the journal entry from those numbers; save it on the row
  ├─ ERP_PENDING       POST ERP journal entry keyed by the same external_reference
  │                    (timeout → look it up by reference first)
  └─ COMPLETED         store the response; return 201
```

The new state is saved before each call to the next system (rule 8). A crash or outage at any step leaves a row that the recovery sweep can resume. The journal entry uses Fineract's own split, never our arithmetic (D-021, D-022).

### Reconciliation (Part K, Part L)

- `POST /api/reconciliation` reads each loan's transactions live from Fineract and the ERP entry with the same `external_reference`. It reports `MATCHED`, `MISSING_IN_ERP`, `MISSING_IN_FINERACT` or `AMOUNT_MISMATCH` and changes nothing (D-024).
- `POST /api/reconciliation/repairs` is the only fix, and it is explicit. It posts a missing ERP entry from Fineract's numbers, through the same idempotent pipeline.
- `GET /api/reconciliation/gl-accounts` compares the two charts of accounts (D-025).

## Data model

### Integration Service (`integration` database)

| Table | Key and constraints | Holds |
|---|---|---|
| `operations` | `id`; **unique `(operation_type, external_reference)`** | One row per state-changing request: payload and its SHA-256 hash, status, attempts, `next_attempt_at`, `locked_until` (lease), `last_error`, the Fineract resource id and ERP entry id it produced, the saved journal-entry request, the stored HTTP response |
| `gl_account_sync` | `external_reference` (PK); unique `erp_code` | Per ERP account: the ERP details and their hash, sync status, attempts, backoff, lease, `last_error`, the Fineract GL account id |
| `client_map` | `client_uid` (PK); unique `fineract_client_id` | Our business id → Fineract's client id, and the operation that created it |
| `loan_map` | `loan_reference` (PK); unique `operation_id`, unique `fineract_loan_id` | Our loan id → Fineract's loan id and the client. `loan_reference` comes from the sequence `loan_reference_seq` (`LN-2026-000001`) |
| `loan_disbursement` | `loan_reference` (PK) | One disbursement slot per loan, owned by one operation (D-026) |

Fineract's numeric ids are stored only to call Fineract. They never appear in a partner response (rule 6, D-015). Loan status and balances are not stored here at all; `GET /api/loans/{ref}` reads them live (rule 7).

### Mock ERP (`erp` database)

| Table | Constraints |
|---|---|
| `accounts` | unique `code`, unique `external_reference` |
| `journal_entries` | unique `external_reference`; entry id `JE-000001` from a sequence; the request hash for replay |
| `journal_lines` | unique `(entry_id, line_number)`; check: exactly one of debit or credit, never negative |

Balance (debits = credits) and known accounts are checked before anything is written (D-020).

### Fineract additions

`acc_gl_account.external_reference` (unique), the `SYNC_GLACCOUNT` permission, and the Liquibase changeset that adds them. Loans, clients and transactions use Fineract's stock `externalId` columns, which are unique: `client_uid` → client, `loan_reference` → loan, `external_reference` → disbursement and repayment transactions.

## Operation state machine

```
RECEIVED ──► FINERACT_PENDING ──► FINERACT_DONE ──► ERP_PENDING ──► COMPLETED
   │               │                   │   │              │
   └──► FAILED ◄───┘                   │   └──────────────┴──► NEEDS_RECONCILIATION
                   └───────────────────┴─────────────────────► NEEDS_RECONCILIATION
                                       └──► COMPLETED   (types with no ERP step only)
```

- **Defined in one place.** Transitions are one table in `domain/states.py`. `check_transition` raises on anything else, and runs under the same row lock that writes the status.
- **Two kinds of operation type.** Types with no ERP step (GL sync, client creation, loan application, approval) go `FINERACT_DONE → COMPLETED`. Disbursements, repayments and repairs must pass through `ERP_PENDING`.
- **What the terminal states mean.**
  - `FAILED`: nothing happened downstream.
  - `NEEDS_RECONCILIATION`: Fineract may have moved money and the ERP side is not confirmed.

## Retries and recovery (no worker)

| Layer | Policy |
|---|---|
| Inline | 3 attempts per call with 0.5 s, 1 s backoff |
| Deferred | `next_attempt_at` = 5 s doubling to a 5 min cap, plus jitter; up to 10 attempts |
| Recovery sweep | Claims due rows with `FOR UPDATE SKIP LOCKED` and a 60 s lease. Runs at startup, every 30 s from an in-process timer, and on `POST /admin/recover` |

**What is safe to retry:**
- A connection failure or a Fineract concurrency 409: Fineract did nothing.
- A timeout: only after looking the resource up by its external reference.
- A rejection is final.

The details, and the five failure scenarios, are in D-023.

## Concurrency

One disbursement per loan rests on five layers (D-026):
1. The operations unique constraint.
2. The per-loan disbursement slot.
3. A row lock on the loan.
4. Fineract's unique transaction `externalId`.
5. Fineract's loan state, which is the last line of defence.

Repayments rely on layers 1 and 4, and on Fineract's own optimistic locking.

## Security and observability

- **Auth.** `X-API-Key`: a partner key for `/api/*`, a separate admin key for `/admin/*` (D-027). The service calls Fineract as a least-privilege user (D-013).
- **Errors.** One shape, `{"error", "code", ...}`, and no Fineract text in it (D-027).
- **Correlation id.** `X-Correlation-ID` on every request and response.
- **Logs.** JSON lines carrying `correlation_id`, `external_reference`, `client_uid`, `loan_reference`, `fineract_transaction_id`, `erp_transaction_id`, `status` and `timestamp` (D-030).

## Source layout

```
integration-service/app/
  api/        thin FastAPI routes: validation, auth, response shape
  services/   pipeline (state machine runner), operation store, handlers per operation type, recovery, reconciliation
  domain/     pure functions: states, idempotency, retry backoff, GL type mapping, accounting mapping, reconciliation
  clients/    typed HTTP clients for Fineract and the ERP
mock-erp/app/                 accounts, journal entries, seed
fineract-customization/       Dockerfile and patch files (Java, Liquibase, JUnit)
scripts/                      bootstrap.py (Fineract setup via REST), simulate_*.py (evidence)
```

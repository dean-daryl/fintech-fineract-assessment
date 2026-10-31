# API

The Integration Service is the only partner-facing API: `http://localhost:8000`. Interactive OpenAPI docs are at `/docs`, and the schema is at `/openapi.json`. The examples below are taken from the evidence logs (`evidence/`) unless they are marked as a shape.

The Mock ERP (`http://localhost:8001`) is described at the end. Partners never call it.

## Conventions

### Authentication

Send `X-API-Key` on every call except `/health`, `/docs` and `/openapi.json` (D-027).

| Routes | Key |
|---|---|
| `/api/*` | `PARTNER_API_KEY` |
| `/admin/*` | `ADMIN_API_KEY` (operators only) |

A missing or wrong key, or the other group's key, gets `401 {"error": "missing or invalid X-API-Key", "code": "unauthorized"}`.

### Correlation id

Send `X-Correlation-ID` to choose the id. Otherwise the service generates one. It is returned on every response, including errors and 500s, and it is in every log line the request produces (D-030).

### Identifiers

Callers only ever see and send business identifiers, never Fineract's numeric ids (rule 6, D-015).

| Identifier | Format | Issued by | In Fineract |
|---|---|---|---|
| `client_uid` | `SME-` + 6 digits | the partner | client `externalId` |
| `loan_reference` | `LN-2026-` + 6 digits | this service, on application | loan `externalId` |
| `external_reference` | 1–100 characters | the partner, one per request | transaction `externalId` (disbursement, repayment) |

### Amounts and dates

- Amounts are whole RWF, as integers (D-009).
- Dates are ISO `YYYY-MM-DD`. A date left out means today in Africa/Kigali.

### Errors

Every error is one flat JSON object (D-027):

```json
{"error": "loan LN-2026-999999 not found", "code": "not_found"}
```

| HTTP | `code` | When |
|---|---|---|
| 401 | `unauthorized` | missing or wrong `X-API-Key` |
| 404 | `not_found` | unknown client, loan or reference |
| 409 | `conflict` | `external_reference` reused with another payload; a loan already disbursed; `client_uid` already registered; Fineract refused a state change |
| 409 | `ambiguous_reference` | `GET /api/transactions/{ref}` when several operation types use the reference |
| 422 | `invalid_request` | request validation; adds `details: [{"in", "field", "message"}]` |
| 422 | `unprocessable` | a business rule, e.g. an amount above Fineract's outstanding balance |
| 502 | `upstream_error` | Fineract refused our own credentials |
| 503 | `service_unavailable` | Fineract or the ERP could not be reached on every attempt |
| 500 | `internal_error` | an unexpected error; logged with its stack trace |

```json
{"details": [{"in": "body", "field": "client_uid", "message": "String should match pattern '^SME-\\d{6}$'"}],
 "error": "the request is not valid", "code": "invalid_request"}
```

**Failed operations** keep their context next to `error` and `code`:

```json
{"external_reference": "SIM-450991-DISBURSE-0", "loan_reference": "LN-2026-000001", "operation_status": "FAILED",
 "error": "loan LN-2026-000001 is already disbursed or being disbursed by another request", "code": "conflict"}
```

When Fineract causes a failure, the message is fixed ("Fineract was unavailable on every attempt") or carries only Fineract's error code ("Fineract rejected the request: error.msg.loan.charged.off"). Fineract's own text can contain its numeric ids, so it stays internal.

## Idempotency contract

Every state-changing request carries an `external_reference`. Uniqueness is enforced by the database, on `(operation_type, external_reference)` (D-014).

| Case | Response |
|---|---|
| New reference | Processed once. **201** with the result |
| Same reference, same payload, finished | **200** with the original body. A failure replays its original status and body |
| Same reference, same payload, still in progress | **202** with the current `operation_status`. Never a second execution |
| Same reference, different payload | **409** `conflict` |

What is unique for each operation type:

| Operation | Unique on | Notes |
|---|---|---|
| GL sync | `external_reference` | Each ERP account is also unique on its own reference and code, in our table, the ERP and Fineract |
| Client creation | `external_reference` | `client_uid` is unique in our database: reusing it under another reference gets 409 |
| Loan application | `external_reference` | Issues one `loan_reference` |
| Approval | `external_reference` | Approving an already approved loan with a new reference returns success; nothing runs twice (D-017) |
| Disbursement | `external_reference` **and one per loan** | A different reference for an already disbursed loan gets 409 (D-026) |
| Repayment | `external_reference` | Many repayments per loan; each reference applies once |

Retrying after a failure needs a new `external_reference`, because a failed request replays its failure.

**202 means accepted, not failed.**
- The body's `operation_status` says how far the operation got: `RECEIVED`, `FINERACT_PENDING`, `ERP_PENDING`, or `NEEDS_RECONCILIATION` if the ERP side could not be confirmed.
- The service finishes it in the background (D-023).
- The same request, or `GET /api/transactions/{external_reference}`, shows the outcome.

## Endpoints

| Method | Path | Purpose | Part |
|---|---|---|---|
| POST | `/api/gl-accounts/sync` | Sync ERP accounts into Fineract GL | A |
| GET | `/api/gl-accounts` | Sync status per ERP account | A |
| POST | `/api/clients` | Create a client | C, F |
| GET | `/api/clients/{client_uid}` | Read a client, live from Fineract | C |
| GET | `/api/clients/{client_uid}/validation?name=` | Exists, active, name matches | C |
| POST | `/api/loans` | Loan application | D, F |
| GET | `/api/loans/{loan_reference}` | Status, balances and transactions, live from Fineract | D |
| POST | `/api/loans/{loan_reference}/approval` | Approve | D, F |
| POST | `/api/loans/{loan_reference}/disbursement` | Disburse (once per loan) | D, F |
| POST | `/api/loans/{loan_reference}/repayments` | Repay | D, F |
| GET | `/api/transactions/{external_reference}` | Any request by its reference | E |
| POST | `/api/reconciliation` | Fineract vs ERP transaction report (read-only) | K |
| POST | `/api/reconciliation/repairs` | Post a missing ERP entry (explicit, idempotent) | K |
| GET | `/api/reconciliation/gl-accounts` | Fineract vs ERP chart of accounts (read-only) | L |
| POST | `/admin/recover` | Run the recovery sweep now | J |
| GET | `/admin/operations/{external_reference}` | Operator view: attempts, `last_error`, lease | J |
| POST, GET, DELETE | `/admin/faults` | Failure injection; only with `FAULT_INJECTION_ENABLED=true` | J |
| GET | `/health` | Liveness | |

Three names differ from the brief: `/api/gl-accounts/sync` (not `/api/accounts/sync`), `/disbursement` (not `/disburse`), and a separate `/approval`. The reasons are in D-018.

### POST /api/gl-accounts/sync

```json
{"external_reference": "DOCS-GL-SYNC-1"}
```

The service reads every ERP account, registers new ones and pushes every `SYNC_PENDING` one to Fineract. It returns each account's status. A per-account failure doesn't fail the request (D-019).

```json
{"external_reference": "DOCS-GL-SYNC-1", "operation_status": "COMPLETED",
 "accounts": [{"external_reference": "GL-1100", "erp_code": "1100", "status": "SYNCED", "attempts": 1, "last_error": null,
               "updated_at": "2026-10-31T12:49:41.159113+00:00"}, "..."]}
```

Each account's `status` is one of these:

| Status | Meaning |
|---|---|
| `SYNC_PENDING` | Retried with backoff |
| `SYNCED` | The Fineract GL account exists |
| `FAILED` | Rejected, or 10 attempts used |
| `CONFLICT` | The ERP account changed after it was registered; sync is create-only |

`GET /api/gl-accounts` returns the same rows, live.

### POST /api/clients

```json
{"external_reference": "SIM-450991-CLIENT", "client_uid": "SME-450991", "name": "Simulation Traders 450991 Ltd", "activation_date": "2026-03-01"}
```
```json
201 {"external_reference": "SIM-450991-CLIENT", "client_uid": "SME-450991", "name": "Simulation Traders 450991 Ltd", "operation_status": "COMPLETED"}
```

- The client is created and activated in Kigali Main Office.
- 409 if the `client_uid` is already registered under another reference, or if Fineract already has it under a different name.

### GET /api/clients/{client_uid} and /validation

- `GET /api/clients/SME-450991` returns `{client_uid, name, active, status, office, activation_date}`, read live from Fineract.
- `/validation?name=...` returns `{"client_uid": "SME-450991", "valid": true, "exists": true, "active": true, "name_matches": true}`.

### POST /api/loans

```json
{"external_reference": "SIM-450991-APPLY", "client_uid": "SME-450991", "client_name": "Simulation Traders 450991 Ltd", "principal": 5000000, "submitted_on_date": "2026-04-01"}
```
```json
201 {"external_reference": "SIM-450991-APPLY", "loan_reference": "LN-2026-000001", "client_uid": "SME-450991", "principal": 5000000, "operation_status": "COMPLETED"}
```

- The loan is on product "SME Asset Finance": 18% a year, 12 monthly repayments, single disbursement (D-012).
- The client must have been onboarded through this API, and it must be active with a matching name.

### POST /api/loans/{loan_reference}/approval

```json
{"external_reference": "SIM-450991-APPROVE", "approved_on_date": "2026-04-01"}
```
```json
201 {"external_reference": "SIM-450991-APPROVE", "loan_reference": "LN-2026-000001", "operation_status": "COMPLETED", "approved_on_date": "2026-04-01"}
```

### POST /api/loans/{loan_reference}/disbursement

```json
{"external_reference": "SIM-450991-DISBURSE-4", "disbursed_on_date": "2026-04-01"}
```
```json
201 {"external_reference": "SIM-450991-DISBURSE-4", "loan_reference": "LN-2026-000001", "operation_status": "COMPLETED",
     "amount": 5000000, "interest_portion": 0, "principal_portion": 0, "transaction_date": "2026-04-01",
     "outstanding_after": 5000000, "erp_transaction_id": "JE-000001"}
```

- The full approved principal is disbursed once.
- Fineract's transaction and the ERP journal entry (Dr 1200 Loan Receivable / Cr 1100 Cash at Bank) both carry the `external_reference`.
- Any other reference for the same loan gets 409.

### POST /api/loans/{loan_reference}/repayments

```json
{"external_reference": "SIM-450991-REPAY-1", "amount": 500000, "transaction_date": "2026-05-01"}
```
```json
201 {"external_reference": "SIM-450991-REPAY-1", "loan_reference": "LN-2026-000001", "amount": 500000, "operation_status": "COMPLETED",
     "principal_portion": 383400, "interest_portion": 116600, "transaction_date": "2026-05-01",
     "outstanding_after": 4616600, "erp_transaction_id": "JE-000002"}
```

- `principal_portion` and `interest_portion` are Fineract's own allocation.
- The ERP entry is built from them: Dr 1100 / Cr 1200 principal / Cr 4100 interest (D-021).
- An amount above Fineract's live total outstanding gets 422, so a loan closes at exactly zero.

### GET /api/loans/{loan_reference}

This reads everything live from Fineract. Loan status is never stored here (rule 7).

```json
{"loan_reference": "LN-2026-000001", "client_uid": "SME-450991", "principal": 5000000, "status": "Closed (obligations met)",
 "balances": {"principal_disbursed": 5000000, "principal_paid": 5000000, "principal_outstanding": 0,
              "interest_charged": 500799, "interest_paid": 500799, "interest_outstanding": 0,
              "total_repaid": 5500799, "total_outstanding": 0},
 "transactions": [{"external_reference": "SIM-450991-REPAY-1", "type": "Repayment", "transaction_date": "2026-05-01",
                   "amount": 500000, "principal_portion": 383400, "interest_portion": 116600,
                   "outstanding_after": 4616600, "reversed": false}, "..."]}
```

The list includes Fineract's own `Accrual` transaction, with `external_reference: null` (D-021).

### GET /api/transactions/{external_reference}

Returns any request by its reference: its type, status, attempts, timestamps, and the `result` the partner got, with the current status on top (D-028).

```json
200 {"external_reference": "FIN-2026-000001", "operation_type": "REPAY_LOAN", "operation_status": "COMPLETED", "attempts": 0,
     "created_at": "2026-10-31T13:20:29.230894Z", "updated_at": "2026-10-31T13:20:29.695418Z",
     "result": {"amount": 500000, "loan_reference": "LN-2026-000008", "principal_portion": 383400, "interest_portion": 116600,
                "outstanding_after": 4616600, "transaction_date": "2026-05-01", "erp_transaction_id": "JE-000051",
                "operation_status": "COMPLETED", "external_reference": "FIN-2026-000001"}}
```

- 404 `not_found` for an unknown reference.
- 409 `ambiguous_reference` (with `operation_types`) when one reference is used by several operation types. Add `?operation_type=REPAY_LOAN` to choose one.

### POST /api/reconciliation

Send either `{"external_references": [...]}` (1–500 references) or `{"from_date": "2026-09-11", "to_date": "2026-09-13"}`. The report is read-only (D-024).

One row per Fineract disbursement or repayment, matched to the ERP entry with the same `external_reference`:

| Status | Meaning |
|---|---|
| `MATCHED` | In both, same amount, type and lines |
| `MISSING_IN_ERP` | In Fineract only |
| `MISSING_IN_FINERACT` | In the ERP only |
| `AMOUNT_MISMATCH` | In both, different amounts |

A reversed Fineract transaction expects a mirror ERP entry keyed `<reference>-REV`.

Shape:

```
{"criteria": {...}, "loans_scanned": 6,
 "summary": {"MATCHED": 3, "MISSING_IN_ERP": 2, "MISSING_IN_FINERACT": 1, "AMOUNT_MISMATCH": 1},
 "items": [{"external_reference", "loan_reference", "status", "operation_status", "fineract": {...}, "erp": {...},
            "differences": ["amount: Fineract 250000, ERP 225000", "1200 credit: Fineract 186588, ERP 161588"], "note"}],
 "not_found": ["SIMK-451114-NEVER-HAPPENED"]}
```

If Fineract or the ERP can't be reached, the call returns 503 and no partial report.

### POST /api/reconciliation/repairs

```json
{"external_reference": "SIMK-451114-FIX-1", "loan_reference": "LN-2026-000006", "transaction_reference": "SIMK-451114-BRANCH-1"}
```
```json
201 {"external_reference": "SIMK-451114-FIX-1", "loan_reference": "LN-2026-000006", "transaction_reference": "SIMK-451114-BRANCH-1",
     "operation_status": "COMPLETED", "amount": 300000, "transaction_type": "Repayment", "principal_portion": 272351,
     "interest_portion": 27649, "transaction_date": "2026-09-13", "reversed": false, "erp_transaction_id": "JE-000042"}
```

- A repair only posts what is missing in the ERP, built from Fineract's numbers.
- It is refused for `AMOUNT_MISMATCH` (409) and `MISSING_IN_FINERACT` (404). Those need a person.
- It is idempotent on its own reference, and per transaction through the ERP's unique reference.

### GET /api/reconciliation/gl-accounts

Compares the two charts of accounts, read-only (D-025). Each account gets one of:

| Status | Meaning |
|---|---|
| `MATCHED` | Same on both sides |
| `DETAILS_MISMATCH` | Both sides have it, with different details |
| `MISSING_IN_FINERACT` | In the ERP only |
| `MISSING_IN_ERP` | In Fineract only |
| `CODE_CONFLICT` | The same code is used by different accounts |

### Admin

- `POST /admin/recover` returns `{"gl_accounts_processed": n, "operations_processed": n}`.
- `GET /admin/operations/{ref}` shows `last_error`, `next_attempt_at` and `locked_until`, which the partner API hides.
- `/admin/faults` arms a fault (`FINERACT_DOWN`, `FINERACT_TIMEOUT_AFTER_SEND`, `ERP_TIMEOUT_AFTER_WRITE`, `CRASH_BEFORE_ERP`) for one reference (D-023).

## Mock ERP

Internal only: the integration service calls it, partners don't. It has no authentication, and its errors use FastAPI's `{"detail": ...}` (D-020).

| Method | Path | Notes |
|---|---|---|
| POST | `/api/accounts` | `{code, name, type, currency, external_reference}`. Idempotent on `external_reference`: same details 200, different 409 |
| GET | `/api/accounts` | The chart; 9 accounts are seeded at startup |
| GET | `/api/accounts/{external_reference}` | One account |
| GET | `/api/accounts/{code}/balance` | Signed by the account type's normal side |
| POST | `/api/journal-entries` | `{external_reference, entry_date, description, currency, lines: [{account_code, debit \| credit}]}`. Unbalanced entries, unknown accounts, a single line, or both sides on one line get 422. Idempotent on `external_reference` |
| GET | `/api/journal-entries` | All entries |
| GET | `/api/journal-entries/{external_reference}` | One entry, `JE-000001` style id |

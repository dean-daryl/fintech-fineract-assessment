---
name: fineract-rest-api
description: Use when Python code (bootstrap script, integration service, simulations) calls the Apache Fineract REST API: auth, tenant header, get-or-create setup, loan lifecycle commands, and external-id lookups.
---

# Calling Fineract from Python

Endpoint paths, enum codes and required fields vary between Fineract releases. Always confirm against the API docs or source of the pinned tag before relying on a field. Never guess a field name; check it, then put it in the typed client.

## Connection basics

- Base URL: `https://<host>:8443/fineract-provider/api/v1`.
- Every request sends `Fineract-Platform-TenantId: default` and HTTP basic auth.
- Local images use a self-signed certificate. Trust it through explicit config (a CA path or a dev-only flag), documented in the README. Never disable verification silently in shared code.
- Send `locale` and `dateFormat` with any payload that contains dates or decimal numbers.
- Wrap all calls in one typed client module (`clients/fineract.py`). Routes and services never build Fineract URLs themselves.

## Credentials

- Admin credentials: bootstrap script only.
- Integration service: the least-privilege API user that bootstrap creates. Read the password from an environment variable.

## Setup (bootstrap) rules

- REST API only, never direct SQL.
- Every step is get-or-create on a natural key (office name, currency code, role name, username, product shortName).
- If a resource exists with a different configuration, fail loudly. Do not silently change it.
- `PUT /currencies` replaces the whole list: read, add RWF, send the merged list.
- Order: health wait, office, currency, role and user, GL sync through the integration service, loan product.
- A cash-accounting loan product needs more GL mappings than Bank, Loan Receivable and Interest Income (fee income, penalty income, write-off, overpayment liability, suspense). Seed those in the ERP so they arrive through GL sync.
- Running bootstrap twice must change nothing on the second run.

## Loan lifecycle

- Every Fineract resource created on behalf of a partner gets `externalId` = our business reference (client_uid, loan_reference, transaction external_reference).
- Application, approve command, disburse command, repayment transaction. Fineract closes the loan itself when outstanding reaches zero.
- Take principal and interest portions from Fineract's transaction response. Never compute them independently.
- The final payment uses Fineract's own total outstanding.
- Dates must satisfy submitted <= approved <= disbursed.

## Timeouts and lookups

- A timeout does not mean failure. Before resending any state-changing call, look up the resource or transaction by externalId.
- Classify responses: connection error or 502/503 means retry; timeout means look up first; 4xx business errors (already disbursed, invalid state) mean do not retry.

## Never

- Expose Fineract numeric IDs as primary identifiers in our API.
- Keep a second copy of loan status in the integration service as a source of truth. Read it live from Fineract.

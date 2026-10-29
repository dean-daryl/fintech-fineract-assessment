# Fineract + ERP integration assessment

This is a take-home assessment, not a production banking platform. Graders evaluate correctness of the Core and Reliability tiers over breadth or polish. Scope carefully.

## System

- **Fineract** (Java/Spring Boot): built from source at a pinned release tag with our patch applied. It owns loan records, loan status, balances and the principal/interest split. It is the source of truth for loans.
- **Mock ERP** (Python/FastAPI): owns the chart of accounts and journal entries. Rejects unbalanced entries. Enforces its own uniqueness in the database.
- **Integration Service** (Python/FastAPI): the only partner-facing API. Owns idempotency, the operation state machine, GL sync, ERP-to-Fineract type mapping, accounting mapping, recovery and reconciliation. It calls Fineract and the ERP; they never call each other.

## Non-negotiable rules

1. **Idempotency is enforced by the database.** Every state-changing request carries an `external_reference`. Uniqueness lives in DB constraints, never in memory, caches or app-level checks alone.
2. **Same reference, same payload** returns the original result (200, or 202 while in progress). **Same reference, different payload** returns 409. Never execute twice.
3. **Disbursement happens once per loan**, even with different references: row lock plus a unique constraint per loan. Fineract's loan state is the last line of defence.
4. **A timeout is not a failure.** Before resending any state-changing call to Fineract or the ERP, look the resource up by its external reference.
5. **Accounting numbers come from Fineract.** Journal entries use Fineract's actual principal and interest portions. Never compute them independently.
6. **Never expose Fineract numeric IDs** as primary identifiers in our API. Use `client_uid`, `loan_reference` and `external_reference`.
7. **Do not keep a second copy of loan status** as a source of truth in the integration service. Read it live from Fineract.
8. **Persist state before calling the next system.** The operation state machine is `RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> ERP_PENDING -> COMPLETED`, plus `FAILED` and `NEEDS_RECONCILIATION`. Transitions are defined in one place and invalid ones raise.
9. **No message broker or separate worker.** Retries and recovery use the operations table: inline retries, deferred retries with backoff, and a recovery sweep (`FOR UPDATE SKIP LOCKED` plus a lease) run on startup, from an in-process timer, and from `POST /admin/recover`.
10. **Fineract setup goes through the REST API only**, from `scripts/bootstrap.py`, as get-or-create steps. No SQL seeding and no Java seed code. Running bootstrap twice changes nothing.
11. **The Java patch stays small and targeted.** It only adds the GL account external reference, its uniqueness and conflict rules, the permission, a Liquibase changeset, and JUnit tests. Schema changes are Liquibase changesets only.
12. **GL accounts come from the ERP** through the integration service's sync. Sync is create-only.
13. **The Fineract database starts empty.** No pre-seeded clients, loans or GL accounts.

## Versions

Fineract tag, JDK, Gradle, database and Python versions are pinned in README.md. Read them from there; never guess or upgrade them. Fineract endpoints and fields vary by release, so check the pinned tag's API docs or source before using a field.

## Repository layout

```
docker-compose.yml  Makefile  README.md  ARCHITECTURE.md  API.md  SIMULATIONS.md  DECISIONS.md
fineract-customization/   Dockerfile, patch/*.patch, Java tests, build notes
mock-erp/                 FastAPI app, Dockerfile, tests
integration-service/      FastAPI app, Dockerfile, tests
scripts/                  bootstrap.py, simulate_*.py
postman/                  collection.json
evidence/                 build logs, simulation output
```

The Fineract source clone lives in `fineract-src/` (gitignored, never committed). Only patch files are committed here.

## Commands

- `make up` / `make down` / `make reset` (down with volumes)
- `make bootstrap`
- `make build-fineract` (regenerates patches from the Fineract clone, then builds the image)
- `make test-java`, `make test-python`, `make lint` (type checker + linter must pass)
- `make simulate-*` for each evidence scenario

## Working rules

- Finish Tier 1 (Core) before starting Tier 2 (Reliability). Stretch work only after both.
- Small, incremental commits with conventional messages (`feat:`, `fix:`, `test:`, `docs:`, `build:`, `chore:`).
- Every behaviour claimed in the docs must be backed by re-runnable evidence in `evidence/` and SIMULATIONS.md. Never claim what the evidence does not show.
- Record every design choice, assumption and known weakness in DECISIONS.md as you go, not at the end.
- Python: type hints everywhere; typed clients for Fineract and the ERP; routes stay thin; pure functions for mappings so they can be unit tested.
- Structured JSON logs with `correlation_id`, `external_reference`, `client_uid`, `loan_reference`, `fineract_transaction_id`, `erp_transaction_id`, `status`, `timestamp` where they apply.
- AI assistance must be disclosed in the README.

## Red flags to avoid

Idempotency only in memory. Blind retries of financial calls. Journal entries computed independently of Fineract. Leaking Fineract internal IDs. A single giant commit. Docs that claim more than the evidence shows.

## Skills

- `fineract-java-patch`: any change to Fineract's Java code, Liquibase, Java tests or the patch file.
- `fineract-rest-api`: any Python code that calls Fineract.
- `idempotent-integration-ops`: any state-changing endpoint, retry/recovery logic, concurrency test or simulation evidence.

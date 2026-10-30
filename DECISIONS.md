# DECISIONS

## D-001: Fineract 1.16.1

Pinned to tag `1.16.1` (commit `c643dab3`), the latest release when work started (2026-10-28). The build also checks the commit hash, so a re-pointed tag fails the build instead of silently changing the source.

Consequence: the tag's Gradle toolchain requires JDK 25 and Gradle 9.7.1 (wrapper).

## D-002: Build Fineract inside Docker

Fineract is compiled in a multi-stage Docker build on `eclipse-temurin:25.0.4.1_1-jdk`, so the stock and patched builds use the same toolchain on any machine. A local JDK 25 is still needed for `make test-java` and for work in the Fineract clone (`fineract-src/`, gitignored).

## D-003: One PostgreSQL server, one database per service

PostgreSQL 18.3 (the version used by the 1.16.1 tag's own compose files; first pinned as 16.15, changed before any data existed) hosts `fineract_tenants`, `fineract_default`, `erp` and `integration` as separate databases. Fineract 1.16.1 supports PostgreSQL, and the integration service's recovery sweep relies on `FOR UPDATE SKIP LOCKED`. Services never read each other's databases.

Known weakness: one server is a shared failure point. Acceptable for an assessment stack.

## D-004: Fineract image build settings

- Only `:fineract-provider:bootJar` is built, skipping tests (`-x test -x cucumber`). `buildJavaSdk` is deliberately not skipped: it generates the Avro classes `fineract-core` needs. Upstream's Docker CI skips it only because it reuses a pre-built workspace; copying that exclusion broke our first clean build. Fineract's own test suite is not run in the image build; our patch's tests run separately via `make test-java`.
- The tag's `gradle.properties` requests a 12 GB Gradle heap. The build overrides only the heap (`GRADLE_XMX`, default 4g) and caps workers at 2, so it fits an 8 GB Docker Desktop. The other JVM flags from the tag are kept unchanged.
- The image runs the jar with `java -jar` on Temurin 25 JRE as user `nobody`, with the same JVM flags as the tag's Jib config. We do not use Jib, because the Dockerfile must apply our patches between clone and build.

## D-005: How patches reach the image

- The Java change is committed in a Fineract clone at `fineract-src/` (gitignored, so it is never committed here; kept inside the project so it opens alongside it in the IDE) and exported with `git format-patch <tag>` into `fineract-customization/patch/`. Only those files are committed here.
- The Dockerfile applies them with `git apply --index`, not `git am`: `git am` needs a committer identity inside the container and adds nothing to the image. Consequence: the jar's `git.properties` still reports the tag's commit, so the image is identified by tag + patch files, not by a commit hash of our own.
- Patches apply in file-name order and the build fails on the first one that does not apply cleanly, so an image can never silently lack part of the change.
- `make build-fineract` regenerates the patches from the clone when one exists (`FINERACT_SRC`, default `fineract-src`) and otherwise uses the committed ones, so graders need no clone. `--no-signature` keeps regenerated files stable across git versions.

## D-006: Compose stack configuration

- Fineract settings are adapted from the tag's `config/docker/env/*.env` into `fineract/configuration/fineract.env`. Removed: the remote debugger (`-agentlib:jdwp` on port 5000), the `test` Spring profile (it enables internal test-only APIs such as `InternalClientInformationApiResource` and `InternalLoanInformationApiResource`), the `diagnostics` profile, the Prometheus endpoint, and the MySQL-only Hikari datasource properties. Management endpoints are limited to `health,info`.
- Tenant timezone is `Africa/Kigali` (upstream's demo uses `Asia/Kolkata`). It sets Fineract's business date, which loan transaction dates must respect.
- Each service gets its own database login and owns only its own database(s): `fineract` (fineract_tenants, fineract_default), `erp`, `integration`. Passwords live in a git-ignored `.env`; `.env.example` is committed.
- Postgres is published on host port 5433 (`DB_HOST_PORT`) so it does not clash with a local postgres on 5432. Services use `db:5432` internally.
- `integration-service` starts only after Fineract and the ERP are healthy, since it is the only service that calls them.
- `make up` uses `docker compose up --wait` with a 15-minute timeout because Fineract's first boot runs every Liquibase changeset.

## D-007: GL account sync in Fineract (Java patch)

**What 1.16.1 lacks.** GL accounts have no field for an external reference. `gl_code` is unique, but it's Fineract's own code, not the ERP's ID. So the patch adds the reference and the sync behaviour.

**What the patch adds.**
- A column `acc_gl_account.external_reference`, unique, added by a Liquibase changeset (`0270`).
- A new endpoint `POST /glaccounts/sync`, protected by a new permission `SYNC_GLACCOUNT`.
- A lookup `GET /glaccounts/external-reference/{ref}`, so the integration service can check before retrying.

**How sync behaves.**

| Request | Result |
|---|---|
| New reference | Account created |
| Same reference, same details | Existing account returned, nothing created |
| Same reference, different details | 409 |
| New reference, but `glCode` already taken | 409 (never adopts an existing account) |

**Why a new command, not a new controller.** It runs through Fineract's normal command pipeline, so permissions, audit log and transactions work the same as the stock create. The stock `POST /glaccounts` is unchanged.

**Concurrent requests.** If two identical syncs race, the database unique constraint stops the second insert. Fineract's built-in command retry then re-runs it, finds the first account and returns it. No duplicate, no error. The violation is recognised by its SQL state (unique violation), not by constraint name: the first version matched names and broke on PostgreSQL, where the `gl_code` constraint is called `acc_gl_account_gl_code_key`, not `acc_gl_code` as the entity says. The 10-parallel-request demo caught it (fix in patch `0002`).

**Not Fineract's job.** Mapping ERP types (`"ASSET"` → `1`) and checking currency happen in the integration service. Fineract GL accounts have no currency.

**Known weaknesses.**
- Stock Fineract still matches `acc_gl_code` by name, so on PostgreSQL a duplicate `glCode` on the stock `POST /glaccounts` returns a generic data-integrity 403 instead of its intended message. Not fixed: outside the patch's scope.
- The response doesn't say whether the account was created or already existed.
- The reference can't be changed after creation. That's on purpose, since sync is create-only.

## D-008: GL account sync (Python side)

**Trigger: an explicit call.** `POST /gl-accounts/sync` on the integration service reads every ERP account and syncs the ones not yet synced. Bootstrap will call it before creating the loan product, so GL accounts always exist first. No webhook (the ERP would need to call us and retry) and no polling loop (it would run constantly for a rare event).

**Status per account** lives in the integration service's `gl_account_sync` table, one row per ERP `external_reference` (the primary key):
`SYNC_PENDING` → `SYNCED`, or `FAILED`.

| Situation | What happens |
|---|---|
| Fineract answers OK | `SYNCED` |
| Fineract down or slow | Stays `SYNC_PENDING`; retried 3 times inline, then later with growing delays (5s, 10s, 20s … up to 5 min) |
| Fineract times out | Looks the account up by reference before resending |
| Fineract rejects (409, 400, 403) | `FAILED` with the reason; not retried |
| Unknown type, wrong currency, name > 45 chars | `FAILED`; never sent to Fineract |
| 10 attempts without success | `FAILED` |

**Retries without a worker.** A recovery sweep picks up due `SYNC_PENDING` rows on startup, every 30 seconds, and on `POST /admin/recover`. Rows are claimed with `FOR UPDATE SKIP LOCKED` plus a 60-second lease, so two sweeps or two sync calls never process the same account at once.

**Same account sent 4 times → one of each.** The ERP's unique constraints give one ERP account, the sync table's primary key gives one row, and Fineract's own reference check gives one GL account.

**Type mapping** is one pure function in `domain/gl_mapping.py`: ASSET→1, LIABILITY→2, EQUITY→3, INCOME→4, EXPENSE→5. Accounts are created as DETAIL accounts with manual entries allowed.

**Not the operations table.** GL sync has its own simple status model from the brief. The operations table and its state machine are for the loan operations in Part B onwards.

**Known weaknesses.**
- Tables are created with SQLAlchemy `create_all` at startup, not migrations. Fine for an empty assessment database; a real service would use Alembic.
- The integration service used Fineract's stock admin user until Part B; it now uses the least-privilege user from D-013.
- TLS verification to Fineract is off, because Fineract's image ships a self-signed certificate. Dev only.
- `FAILED` is final. Fixing the cause and retrying needs a manual step, which isn't built.

## D-009: Fineract setup is one bootstrap script

`scripts/bootstrap.py` (`make bootstrap`) configures Fineract through the REST API only, in this order: wait for health, office, currency, API role and user, ERP chart of accounts and GL sync, loan product.

- Every step is get-or-create on a natural key: office name, currency code, role name, username, ERP `external_reference`, product `shortName`.
- If a resource exists with a different configuration, the script stops with a non-zero exit and lists the differences. It never updates anything, so a hand-made change is never silently overwritten.
- It runs on the host with only the Python standard library, like the simulation scripts. Credentials come from `.env`.
- Admin credentials (`FINERACT_ADMIN_*`) are used only here. The script refuses to run if the API user and the admin user are the same.
- The API user is created **before** GL sync, so the sync already runs as the least-privilege user. That shows `SYNC_GLACCOUNT` plus `READ_GLACCOUNT` is enough for it.

**Office.** "Kigali Main Office" is created as a child of Fineract's stock "Head Office", with `externalId=OFFICE-KGL-MAIN` and opening date 2026-01-01. The stock Head Office is not renamed: it is Fineract's own root and other stock data refers to it. Loans and clients go in Kigali Main Office.

**Currency.** RWF. Fineract's currency table already has it with 0 decimal places, which matches ISO 4217 (the franc has no minor unit in use). `PUT /currencies` replaces the whole list, so bootstrap reads the list and adds RWF to it. The stock USD stays.

## D-010: Accounting rule is cash-based

The loan product uses `accountingRule=2` (cash based).

**Why.**
- Fineract then writes journal entries only for real cash events: disbursement and repayment. Those are exactly the events the integration service sends to the ERP, using Fineract's own principal/interest split. Each ERP entry matches one Fineract transaction.
- Accrual would add entries that no partner call causes (interest accrued daily or periodically by a scheduled job). Keeping the ERP in step with those would need a separate sync of accrual events. That is outside the brief.
- Cash needs fewer GL mappings. Accrual also needs interest, fee and penalty receivable accounts.

**Weakness.** Interest income is recognised when it is paid, not as it is earned. A regulated lender reporting under IFRS 9 would need accrual. Switching later means a new product (the rule is a product setting), new receivable accounts in the ERP, and syncing accrual entries.

## D-011: Chart of accounts and product GL mappings

The chart of accounts belongs to the ERP. Bootstrap creates it in the ERP (`POST /api/accounts`, which returns 200 for an identical account), then calls `POST /gl-accounts/sync` on the integration service. Bootstrap stops if any account is not `SYNCED`. Fineract GL ids are looked up by external reference and used only inside bootstrap.

All accounts are in RWF, with `external_reference = GL-<code>`:

| Code | Name | Type | Loan product mapping |
|---|---|---|---|
| 1100 | Cash at Bank | ASSET | `fundSourceAccountId` |
| 1200 | Loan Receivable | ASSET | `loanPortfolioAccountId` |
| 1300 | Transfers in Suspense | ASSET | `transfersInSuspenseAccountId` |
| 2100 | Loan Overpayments | LIABILITY | `overpaymentLiabilityAccountId` |
| 4100 | Interest Income on Loans | INCOME | `interestOnLoanAccountId` |
| 4200 | Fee Income | INCOME | `incomeFromFeeAccountId` |
| 4300 | Penalty Income | INCOME | `incomeFromPenaltyAccountId` |
| 4400 | Recoveries Income | INCOME | `incomeFromRecoveryAccountId` |
| 5100 | Loan Losses Written Off | EXPENSE | `writeOffAccountId` |

- The mapped set is what Fineract 1.16.1 requires for cash accounting (`LoanProductDataValidator`). Fee, penalty, recovery and write-off accounts exist only because of that: the product has no fees or penalties, and nothing writes off loans yet.
- One account per mapping, so every journal line in Fineract points at an account whose purpose is clear in the ERP.
- `GL-1200` is the same account as the Part A simulation (`simulate_gl_sync.py`), so both scripts can run on the same stack.
- Optional mappings (goodwill credit, charge-off) are left empty. Fineract allows that, and nothing in the brief uses them.

## D-012: Loan product "SME Asset Finance"

`shortName=SAF`. Every enum code below was checked against the 1.16.1 source (`AccountingRuleType`, `InterestMethod`, `AmortizationMethod`, `InterestCalculationPeriodMethod`, `PeriodFrequencyType`, `DaysInYearType`, `DaysInMonthType`, `RepaymentStartDateType`).

| Setting | Value | Why |
|---|---|---|
| Currency | RWF, 0 decimals, multiples of 1 | D-009 |
| Principal | min 100,000 / default 1,000,000 / max 50,000,000 RWF | Assumed SME asset-finance range; the brief sets none |
| Interest | 18 per year (`interestRateFrequencyType=3`) | Brief |
| Interest method | Declining balance (`interestType=0`) | Interest on the outstanding principal, the usual choice for asset finance. Flat would charge interest on principal already repaid |
| Amortization | Equal installments (`amortizationType=1`) | Same payment every month, the usual choice for an SME borrower |
| Interest period | Same as repayment period (`interestCalculationPeriodType=1`) | Simple, predictable schedule |
| Repayments | 12, every 1 month (`repaymentFrequencyType=2`) | Brief |
| Days in year / month | Actual / actual (`1` / `1`) | Fineract's default; with interest per repayment period it barely changes the schedule |
| Repayment start | From disbursement date (`repaymentStartDateType=1`) | The first installment is due a month after the money goes out |
| Repayment strategy | `mifos-standard-strategy` | Fineract's standard order: penalties, fees, interest, principal |
| Multi-disbursement | Off (`multiDisburseLoan=false`) | Brief: one disbursement per loan |
| Interest recalculation | Off | Not needed for cash accounting and a fixed schedule |
| Charges | None | Brief: no fees, penalties or taxes (stretch item) |
| Accounting | Cash, mappings from D-011 | D-010 |

Bootstrap compares these fields and all 9 GL mappings on an existing product and stops on any difference.

## D-013: Least-privilege API user

Role `INTEGRATION_API` and user `integration-api` (office Kigali Main Office, `passwordNeverExpires`, no password e-mail). The password comes from `FINERACT_API_PASSWORD` in `.env`. The integration service uses only this user (`docker-compose.yml`).

Permissions: exactly what the integration service needs for Parts A to D, and nothing else.

| Permission | Used for |
|---|---|
| `READ_GLACCOUNT`, `SYNC_GLACCOUNT` | GL sync and lookup by reference (Part A) |
| `READ_OFFICE` | Find Kigali Main Office by name for new clients |
| `READ_LOANPRODUCT` | Find SME Asset Finance by short name |
| `CREATE_CLIENT`, `READ_CLIENT` | Partner client onboarding, lookup by external id |
| `CREATE_LOAN`, `APPROVE_LOAN`, `DISBURSE_LOAN`, `REPAYMENT_LOAN`, `READ_LOAN` | Loan lifecycle and live status |
| `READ_JOURNALENTRY` | Reconciliation against Fineract's journal entries |

Not granted: `ALL_FUNCTIONS` or `ALL_FUNCTIONS_READ`, any `UPDATE_*`, `DELETE_*` or undo command, and any admin of products, offices, users, roles, GL accounts or journal entries. Approve and disburse also run as this user: the integration service does them on the partner's behalf, and the brief has no human approver. A real deployment would put approval behind maker-checker.

Bootstrap compares the role's permission set exactly. A role with one permission more or fewer stops the script.

`scripts/verify_fineract_setup.py` checks this as the API user: it can read the office, the product and its GL mappings. It gets 403 on `POST /offices`, `/loanproducts`, `/users`, `/roles`, `/glaccounts` and `/journalentries`.

**Known weaknesses.**
- **Upstream gap: Fineract 1.16.1 does not authorize `PUT /currencies`.** `SecurityConfig` protects `POST /api/*/currencies` with `UPDATE_CURRENCY`, but the endpoint is `PUT`, and `CurrencyUpdateCommandHandler` has no permission check of its own. So **any** authenticated user, this one included, can replace the tenant's currency list. Found by the verify script: a test request as `integration-api` removed USD (put back by hand). Not fixed: rule 11 keeps our Java patch to GL accounts. Mitigation: the integration service never calls that endpoint. The verify script still reports the gap with a harmless probe (an unknown currency code, which the handler rejects without changing anything). The fix belongs upstream: match `PUT` in `SecurityConfig`.
- The stock admin `mifos`/`password` is unchanged. Bootstrap needs it on a fresh Fineract. A real deployment would rotate it after bootstrap.
- The user has a placeholder e-mail (`integration-api@integration.invalid`), because Fineract requires one when `sendPasswordToEmail` is false.
- Bootstrap cannot check that an existing user's password matches `.env` (Fineract never returns it). It logs in as the user after the user step, so a wrong password fails there.

## D-014: Operations table and state machine

Every partner request that changes state goes through one table, `operations`, and one state machine. GL sync keeps its own table (D-008).

**The table is the idempotency store and the retry queue.** One row per request, unique on `(operation_type, external_reference)` in the database (`uq_operations_type_reference`). It holds the request payload and its SHA-256 hash (canonical JSON, sorted keys), status, attempts, `next_attempt_at`, `locked_until`, `last_error`, the Fineract and ERP ids of what the operation created, and the stored response (HTTP code and body).

**Claiming.** A request runs `INSERT ... ON CONFLICT DO NOTHING RETURNING`. Exactly one concurrent request gets the row back and executes. Every other one reads the existing row and replays:

| Existing row | Same payload hash | Different payload hash |
|---|---|---|
| Finished (`COMPLETED`, `FAILED`, `NEEDS_RECONCILIATION`) | Stored response (200 instead of 201 for a success) | 409 |
| Not finished | 202 with the current status | 409 |

A failed operation replays its failure. Retrying a failed request needs a new `external_reference`.

**State machine.** The allowed transitions are one table in `domain/states.py`. `check_transition` raises on anything else, and the store calls it inside the same row lock (`SELECT ... FOR UPDATE`) that writes the new status.

```
RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> ERP_PENDING -> COMPLETED
RECEIVED, FINERACT_PENDING -> FAILED
FINERACT_PENDING, ERP_PENDING -> NEEDS_RECONCILIATION
FINERACT_DONE -> COMPLETED   only for operation types with no ERP step
```

- An operation type with no ERP step (client creation) goes `FINERACT_DONE -> COMPLETED`; one with an ERP step must go through `ERP_PENDING`. This is checked per type, so a loan repayment can never skip its journal entry.
- `FAILED` means nothing happened downstream. `FINERACT_PENDING -> NEEDS_RECONCILIATION` is for when Fineract's outcome stays unknown after every attempt (a timeout, then lookups that also fail).
- The three finished statuses have no transitions out. Resolving `NEEDS_RECONCILIATION` belongs to the reconciliation work, not built yet.

**Lease.** The request that creates the row also takes a lease (`locked_until`, `operation_lease_seconds`). While the lease holds, the recovery sweep skips the row, so the request in flight and the sweep never execute the same operation. A deferred retry releases the lease and sets `next_attempt_at` with the same backoff as GL sync (`domain/retry.py`: 5s doubling to 5 min, plus jitter). The sweep claims due rows with `FOR UPDATE SKIP LOCKED` and takes a fresh lease.

**Known weaknesses.**
- A crash between Fineract succeeding and the store writing `FINERACT_DONE` leaves the row in `FINERACT_PENDING`. Recovery resolves it by looking the resource up by its external id before resending (rule 4), so correctness depends on every operation having such a lookup.
- A request that outlives its lease (a Fineract call longer than the lease) can overlap with the sweep. Fineract's own uniqueness on external ids is the guard behind it. The lease is far longer than the Fineract timeout.
- The payload is stored in full. Fine for client and loan data in an assessment; a real service would review retention of personal data in this table.

## D-015: Business identities and the client mapping table

Partners identify clients and loans only by business ids. Fineract's numeric ids never appear in a request or a response.

| Id | Format | Issued by | Stored in Fineract as |
|---|---|---|---|
| `client_uid` | `SME-` + 6 digits | **The partner**: it is their business id for the SME | client `externalId` |
| `loan_reference` | `LN-<year>-` + 6 digits | The integration service (Part D) | loan `externalId` |

**`client_uid` comes from the partner.** The brief says only that `loan_reference` is issued by our service, and an SME already has an id in the partner's system. We check its format and its uniqueness, and never generate one.

**`client_map`** has one row per `client_uid` (primary key): the id of the operation that owns it, and Fineract's client id (unique, empty until Fineract confirms). It stores no name and no status. Those are read live from Fineract (rule 7), so the two can never disagree. Fineract's id is used only inside the service, for calls such as the loan application in Part D.

**The `client_uid` is reserved before Fineract is called.** The create operation inserts its `client_map` row first, so two requests with different `external_reference`s but the same `client_uid` cannot both reach Fineract. The second finds the row owned by another operation and fails with 409. A `client_uid` whose owning operation `FAILED` (nothing was created in Fineract) can be taken over by a new request, so a partner can fix a rejected request and send it again under a new `external_reference`.

**Two guards, both in a database.** `client_map`'s primary key in ours, and Fineract's own `UNIQUE (external_id)` on `m_client` (checked on the running 1.16.1 database: `m_client_external_id_key`) behind it.

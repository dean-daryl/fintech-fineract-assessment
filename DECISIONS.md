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

**Trigger: an explicit call.** `POST /api/gl-accounts/sync` on the integration service, with an `external_reference` (D-019), reads every ERP account and syncs the ones not yet synced. Bootstrap will call it before creating the loan product, so GL accounts always exist first. No webhook (the ERP would need to call us and retry) and no polling loop (it would run constantly for a rare event).

**Status per account** lives in the integration service's `gl_account_sync` table, one row per ERP `external_reference` (the primary key):
`SYNC_PENDING` → `SYNCED`, or `FAILED`, plus `CONFLICT` (D-019).

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

**Two levels.** Each sync *request* is an operation in the operations table (D-019). Each *account* keeps its own simple status model from the brief in `gl_account_sync`, because one request covers many accounts and an account can outlive the request that first saw it (deferred retries).

**Known weaknesses.**
- Tables are created with SQLAlchemy `create_all` at startup, not migrations. Fine for an empty assessment database; a real service would use Alembic.
- The integration service used Fineract's stock admin user until Part B; it now uses the least-privilege user from D-013.
- TLS verification to Fineract is off, because Fineract's image ships a self-signed certificate. Dev only.
- `FAILED` is final while the ERP account stays the same. If the ERP account changes, the next sync retries it with the new details (D-019).

## D-009: Fineract setup is one bootstrap script

`scripts/bootstrap.py` (`make bootstrap`) configures Fineract through the REST API only, in this order: wait for health, office, currency, API role and user, check the ERP's seeded chart of accounts (D-020) and GL sync, loan product.

- Every Fineract step is get-or-create on a natural key: office name, currency code, role name, username, product `shortName`.
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

The chart of accounts belongs to the ERP, which seeds it at startup (D-020). Bootstrap checks that every account the product maps is in the ERP, then calls `POST /api/gl-accounts/sync` on the integration service. Bootstrap stops if any account is not `SYNCED`. Fineract GL ids are looked up by external reference and used only inside bootstrap.

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
| `CREATE_CLIENT`, `ACTIVATE_CLIENT`, `READ_CLIENT` | Partner client onboarding, lookup by external id. Clients are created active (`active=true`), and Fineract checks `ACTIVATE_CLIENT` for that on top of `CREATE_CLIENT` |
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
FINERACT_PENDING, FINERACT_DONE, ERP_PENDING -> NEEDS_RECONCILIATION
FINERACT_DONE -> COMPLETED   only for operation types with no ERP step
```

`FINERACT_DONE -> NEEDS_RECONCILIATION` is only for types with an ERP step: Fineract moved money but no journal entry could be built (D-022).

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

## D-016: One pipeline, one handler per operation type

`services/pipeline.py` runs every partner operation: claim, replay, prepare, Fineract call, retries, give-up and recovery. It is written once. Each operation type supplies a small handler (`prepare`, `look_up`, `execute`, `record`, `describe`), so client creation and the four loan operations share the same idempotency and retry code instead of five copies.

| Handler step | Runs | Purpose |
|---|---|---|
| `prepare` | Once, in `RECEIVED` | Local guards before Fineract is called: reservations, live validation. Raising `OperationRejected` gives `FAILED` without touching Fineract |
| `execute` | `FINERACT_PENDING` | The state-changing Fineract call |
| `look_up` | After a timeout, on recovery, and when Fineract rejects | Finds the resource by our reference, so a lost response is never re-sent (rule 4) |
| `record` | Before `FINERACT_DONE` | Saves the Fineract id in the mapping table |

- **A Fineract rejection is checked with a lookup first.** If the resource already exists under our reference (an earlier attempt succeeded but its response was lost), the operation completes instead of failing.
- **Fineract's result is saved with `FINERACT_DONE`** (for a repayment: Fineract's principal and interest portions). Completing from `FINERACT_DONE` needs no further Fineract call.
- **Rejection codes.** Fineract 403 (business rule or data integrity, e.g. "already approved") → 409; 404 → 404; other 4xx → 422; Fineract's "not authorized" → 502, because that is our misconfiguration, not the partner's fault.

## D-017: Loan lifecycle (Part D)

**Endpoints.** `POST /api/loans`, `POST /api/loans/{loan_reference}/approval`, `POST /api/loans/{loan_reference}/disbursement`, `POST /api/loans/{loan_reference}/repayments`, and `GET /api/loans/{loan_reference}`. Each `POST` carries its own `external_reference` and is an operation (D-014).

**`loan_reference` is issued by us** from a Postgres sequence (`loan_reference_seq`) as `LN-<year>-<6 digits>`, inside the application's `prepare` step and only once per operation (`loan_map.operation_id` is unique). A repeated application always returns the same `loan_reference`. The year is the year of issue (tenant timezone). The sequence is global rather than per year, so numbers do not restart in January. Gaps are possible (a rejected application still uses a number) and harmless.

**`loan_map`** holds `loan_reference` (primary key), `client_uid`, the owning operation and Fineract's loan id. Like `client_map`, it holds ids only. Status and balances are always read live (rule 7).

**Terms come from the product.** The service reads the SAF product from Fineract (repayments, frequency, rate, interest method, amortization, strategy) and builds the application from it. The partner sends only the principal and dates, so the product stays the single source of the loan's terms.

**Every loan request is checked against our own data before Fineract.**

| Operation | `prepare` guard | Fineract guard behind it | `look_up` |
|---|---|---|---|
| Application | Client was onboarded here, and live validation passes (exists, active, name matches) | `m_loan.external_id` unique | Loan by `loan_reference` |
| Approval | Loan exists | Loan state | Loan's status is approved or later |
| Disbursement | Row lock on `loan_map` + `loan_disbursement` primary key (one slot per loan) | Loan state | Disbursement transaction by `external_reference` |
| Repayment | Live: loan active, amount ≤ Fineract's total outstanding | `m_loan_transaction.external_id` unique | Repayment transaction by `external_reference` |

- **Disbursement happens once (rule 3).** A second disbursement with a different `external_reference` finds the slot owned by another operation and gets 409 before Fineract is called. A slot whose owner `FAILED` can be taken over, so a disbursement Fineract rejected can be sent again.
- **Disbursement and repayment transactions carry our `external_reference` as their Fineract `externalId`.** Both accept it at 1.16.1 (`LoanTransactionValidatorImpl`), and Fineract's unique index on `m_loan_transaction.external_id` is the second guard. The lookup also checks the transaction type, so a reference already used by a disbursement is never mistaken for a repayment.
- **Approval has no transaction to look up**, so its lookup is the loan's status. Consequence: approving an already-approved loan with a new reference returns success, not 409. Nothing executes twice.
- **The overpayment check reads Fineract's live balance** and rejects anything above it (422), so the loan ends "Closed (obligations met)", never "Overpaid". The final payment is Fineract's own `totalOutstanding`, never our arithmetic.
- **Amounts are whole RWF** (no minor unit, D-009). Any Fineract amount with a fractional part raises instead of being rounded silently.
- **Dates.** Partners may send past dates. Fineract allows backdated transactions on this tenant (`disallow-backdated-transactions` is off), and the simulation uses them so the three partial repayments fall on real due dates and carry real interest. Without a date, the service uses today in the tenant timezone.
- **Disbursement and repayment go through the ERP step** (D-022). Client creation, loan application and approval move no money and have none.

**Known weaknesses.**
- Two different repayments that both pass the overpayment check at the same moment could together exceed the balance. Fineract then records an overpayment. A per-loan lock around the Fineract call would close this; not built.
- `GET /api/loans/{loan_reference}` lists every Fineract transaction on the loan. A transaction made outside this service would show `external_reference: null`.
- No reschedule, write-off, undo or charges: outside the brief.

## D-018: Partner routes live under `/api`

**Decision.** Every partner-facing route is mounted under `/api` (`/api/gl-accounts`, `/api/clients`, `/api/loans`). `/health` and `/admin/recover` stay at the root: they are for operators and health checks, not partners, and keeping them apart makes it simple to put a different auth rule or network policy on them later.

**Names that differ from the brief.** `/api/gl-accounts/sync` instead of `/api/accounts/sync`, because the resource is the Fineract GL account and not the ERP account. `POST /api/loans/{loan_reference}/disbursement` instead of `POST /api/loans/{loan_reference}/disburse`, and a separate `POST /api/loans/{loan_reference}/approval`, because each `POST` creates a resource (an operation with its own `external_reference`) and is not a verb on the loan.

## D-019: GL sync requests carry an `external_reference`

**Why.** Part F lists GL sync among the requests that carry an `external_reference` and follow the same replay rules. Until now `POST /api/gl-accounts/sync` took no body, so it had no 200 replay, no 202 while running, and a changed ERP account was ignored without a trace.

**Decision.** The sync request is an operation of type `SYNC_GL_ACCOUNTS` in the operations table, run by the same pipeline as every other operation (D-014, D-016). Body: `{"external_reference": "..."}`.

| Case | Result |
|---|---|
| New reference | Runs once: reads the ERP, syncs pending accounts, 201 with every account's status |
| Same reference, finished | 200 with the original body (the statuses as they were then; `GET /api/gl-accounts` shows them live) |
| Same reference, still running | 202 with `operation_status`; never a second run |
| ERP unreachable | `FAILED`, 503; nothing was sent to Fineract. Replays the 503; a new reference retries |

- **The request has no payload besides the reference**, so "same reference, different payload" cannot happen at the request level. It applies per account, below.
- **Handler.** `prepare` reads the ERP and registers accounts. `execute` pushes every `SYNC_PENDING` account. `look_up` returns nothing: re-running `execute` is safe, because each account is claimed with `SKIP LOCKED` plus a lease, looked up by reference after a Fineract timeout, and Fineract's sync is itself idempotent on the reference (D-007).
- **No ERP step.** The ERP is only read, so `SYNC_GL_ACCOUNTS` goes `FINERACT_DONE -> COMPLETED`.
- **Per-account failures do not fail the request.** An account Fineract rejects is `FAILED`, one Fineract cannot reach is `SYNC_PENDING` with a backoff. The request still completes and reports each status.

**Per account: same reference, different details.** Each `gl_account_sync` row keeps the hash of the ERP account it was registered with. When a later sync reads the same `external_reference` with a different hash:

| Row status | Result |
|---|---|
| `SYNCED`, `SYNC_PENDING` | `CONFLICT`, with the reason in `last_error`. Never sent again; sync is create-only, and the Fineract account keeps its original details |
| `FAILED` | Back to `SYNC_PENDING` with the new details, attempts reset. Nothing was created in Fineract, so the corrected account can be sent (same rule as a failed `client_uid` in D-015) |
| `CONFLICT` | Stays `CONFLICT` |

`CONFLICT` is not overwritten by a push that was already in flight when the change was seen.

**Code is unique in our database too.** `gl_account_sync.erp_code` is now `UNIQUE`, on top of the ERP's and Fineract's own constraints. An ERP account whose code belongs to another synced reference is not registered; the sync logs an error for it.

**Known weaknesses.**
- The mock ERP is create-only and returns 409 for a changed account, so drift can only come from an ERP whose data changes outside its API. The `CONFLICT` path is a guard, not something the current stack can produce through its APIs.
- A code clash is only logged, not shown in the sync response.
- `create_all` does not alter existing tables, so the new unique constraint needs a fresh database (`make reset`).
- Bootstrap uses a new sync reference on every run (`BOOTSTRAP-GL-SYNC-<epoch seconds>`), so a run never replays an earlier failure. Each run adds one operation row; Fineract and the ERP are unchanged.

## D-020: Mock ERP (Part G)

**Endpoints.** `POST /api/accounts`, `GET /api/accounts`, `GET /api/accounts/{external_reference}`, `GET /api/accounts/{code}/balance`, `POST /api/journal-entries`, `GET /api/journal-entries/{external_reference}`.

**The ERP seeds its own chart of accounts.** The 9 accounts of D-011 are inserted at startup with `ON CONFLICT DO NOTHING` (`app/seed.py`). An existing account with different details is logged and left unchanged. Bootstrap no longer defines the chart: it reads `GET /api/accounts` and stops if a mapped account is missing. One copy of the chart, owned by the system that owns it. The brief's examples (1100 Bank, 1200 Loan Receivable, 4000 Interest Income) start with "e.g."; 1100, 1200 and 4100 cover them.

**Journal entries.** `journal_entries` (one row per entry, `external_reference` UNIQUE, the request's SHA-256 hash) and `journal_lines` (account, debit, credit). Amounts are whole RWF in `BIGINT` (D-009). Lines name accounts by `code`, like the balance route. Entries are immutable: no update or delete.

| Request | Result |
|---|---|
| New reference, valid | 201 |
| Same reference, same details | 200 with the stored entry |
| Same reference, different details | 409 |
| Fewer than 2 lines, a line with both or neither side, unknown account, account in another currency, debits ≠ credits | 422; nothing stored |

**What the ERP enforces itself, without trusting the caller.**
- Reference uniqueness is the database's `UNIQUE` constraint. Concurrent posts all try to insert; the one that loses gets the unique violation, rolls back, reads the winner and compares hashes. Same pattern as accounts.
- `CHECK (debit >= 0 AND credit >= 0 AND (debit = 0) <> (credit = 0))` on every line, and `UNIQUE (entry_id, line_number)`.
- Balance (debits = credits) is checked by `app/journal.py` before the insert, and the entry and its lines are written in one transaction. A balance rule in the database would need a deferred constraint trigger across rows; with immutable entries and a single write path, the check in the transaction is enough for a mock.

**Balance** is debits minus credits for ASSET and EXPENSE, credits minus debits for the others, so a normal balance is positive. The response also gives both totals.

**Identifiers.** Accounts are read by `external_reference` (`GET /api/accounts/{external_reference}`, used by GL sync) and by `code` for balances, as in the brief. The two routes have a different number of path segments, so they never clash.

**Evidence script.** `scripts/simulate_erp.py` posts two entries that offset each other (1100 → 1300, then back), so balances end where they started and a re-run does not distort later accounting. Its references start with `SIM-ERP-`.

**Known weaknesses.**
- No reversal endpoint and no period close. A wrong entry can only be corrected by a new, offsetting entry, which is also how a real ledger works.
- The line order is part of the hash: the same lines in another order are a different payload (409).
- `create_all` creates the new tables on an existing database, but the chart only exists after the ERP restarts with the seed.

## D-021: Accounting mapping (Part H)

**Decision.** `app/domain/accounting.py` turns one Fineract loan transaction into one ERP journal entry. `journal_entry_for(transaction, loan_reference)` is a pure function with no I/O, unit-tested in `tests/test_accounting.py`. It takes the transaction as `parse_loan_transaction` reads it from Fineract and never computes an amount itself.

**Where the principal/interest split comes from.** Fineract allocates every repayment when it books it. The loan's transaction processing strategy (`mifos-standard-strategy`, D-012) applies the payment to the schedule in the order penalties, fees, interest, principal, using the installments that are due on the transaction date. Fineract stores the result on the transaction (`principalPortion`, `interestPortion`, `feeChargesPortion`, `penaltyChargesPortion`, `overpaymentPortion`), and `GET /loans/external-id/{loan_reference}/transactions/external-id/{external_reference}` returns it. The mapping copies those portions. It does not know the rate, the schedule or the allocation order, so it cannot disagree with Fineract (rule 5). Fineract's own cash-accounting journal for the same transaction uses the same portions, which a later reconciliation can compare line by line (`GET /journalentries?transactionId=L<id>`).

**Lines** (account codes from D-011):

| Fineract transaction | Debit | Credit |
|---|---|---|
| Disbursement | 1200 Loan Receivable: `amount` | 1100 Cash at Bank: `amount` |
| Repayment | 1100 Cash at Bank: `amount` | 1200 Loan Receivable: `principalPortion`; 4100 Interest Income: `interestPortion`; 2100 Loan Overpayments: `overpaymentPortion` |

- **One entry per Fineract transaction.** The entry's `external_reference` is the transaction's `externalId`, which is our operation's `external_reference` (D-017). The ERP's unique reference then means a resend can never post twice. The later ERP step can look the entry up by that reference before resending (rule 4).
- **The entry date is Fineract's transaction date**, not today, so a backdated repayment lands in the right period.
- **The description names the `loan_reference`**, never a Fineract id (rule 6).
- **Zero lines are left out.** A repayment with no interest has no 4100 line. The ERP rejects lines with neither a debit nor a credit.
- **The mapping refuses instead of guessing.** It raises `AccountingMappingError` when:
  - the portions do not add up to the amount
  - a repayment carries fee or penalty portions (SAF has no charges, D-012)
  - a disbursement carries any portion other than principal
  - the transaction is reversed, or has no external reference, no date or a non-positive amount
  - the type is not a disbursement or a repayment (accruals, waivers, write-offs and the rest)

  Plugging a difference into some account would hide exactly the disagreement the books exist to catch. The function also checks that the entry balances before returning it; the ERP checks again on its side.

**Overpayments.** The repayment route rejects any amount above Fineract's live outstanding balance (D-017), so in normal use no overpayment portion exists. The two-concurrent-repayments race in D-017 can still produce one. Fineract then books the excess as `overpaymentPortion`, and the mapping credits it to 2100 Loan Overpayments, a liability: money owed back to the client, not income. This matches the product's `overpaymentLiabilityAccountId` mapping (D-011). Refunding it would be a separate Fineract transaction (credit balance refund), mapped DR 2100 / CR 1100; not built.

**Reversals.** ERP entries are immutable (D-020), so a reversed Fineract transaction is never edited or deleted in the ERP. `reversal_of(entry, reversed_on)` builds the offsetting entry: the same accounts with debits and credits swapped, reference `<original>-REV`, dated on the day of the reversal. The original entry plus its reversal net to zero, and both stay in the audit trail. If Fineract then re-processes the loan (a reversed earlier repayment can change how later repayments were split), each changed transaction gets its own reversal and a new entry with its new portions. The service has no undo endpoint (D-017), so `reversal_of` is tested but not connected to anything yet.

**Known weaknesses.**
- Fees, penalties, waivers, write-offs and accruals have no mapping; they raise. The product has none of them (D-010, D-012).
- The account codes are constants matching D-011. If the product's GL mappings change in Fineract, this module must change with them. Reading the product's mappings at runtime would remove that coupling; not built.
- Fineract 1.16.1 books an `Accrual` transaction on a loan even under cash accounting: 500,799 RWF, the loan's whole scheduled interest, with no `externalId`, dated at disbursement. Cause: the global configuration `allow-cash-and-non-cash-accrual` is on by default on a fresh tenant ("accrual transactions are created at disbursement for None, Cash, and Upfront Accrual accounting types (legacy behavior)"; `LoanDisbursementService`, `LoanAccrualsProcessingServiceImpl`). Bootstrap leaves it at the default. Under cash accounting Fineract posts no journal entry for it, and it doesn't change the loan's outstanding balance, which already includes scheduled interest. The mapping never sees it: it only maps the transaction an operation created, looked up by that operation's `external_reference`. It would raise if it were given one. Reconciliation skips it, because it keys on disbursements and repayments only (D-024).

## D-022: ERP posting step

**Decision.** Disbursements and repayments end with exactly one ERP journal entry. The pipeline runs `FINERACT_DONE -> ERP_PENDING -> COMPLETED` for them (`services/pipeline.py`). The journal entry comes from Part H's mapping (D-021), applied to the Fineract transaction the operation created.

**The entry is saved before the ERP is called (rule 8).** `journal_entry_for` runs when Fineract's outcome is known. The resulting request is written to `operations.erp_request` in the same transaction that sets `FINERACT_DONE`. Every attempt, inline or from recovery, sends those saved bytes. So:
- a resend is byte-identical, and the ERP answers it 200 with the stored entry, never 409;
- the ERP step does not need Fineract to be up;
- the numbers are Fineract's numbers at the moment of execution (rule 5).

**Look up before resending (rule 4).** After an ERP timeout, and on every recovery pass, the pipeline first reads `GET /api/journal-entries/{external_reference}`. If the entry is there, it is used. If the entry found differs from `erp_request`, the operation goes to `NEEDS_RECONCILIATION`, never overwritten. A plain "connection refused" is resent without a lookup, because nothing reached the ERP. The ERP's unique reference (D-020) is the guard behind both.

| ERP answer | What the pipeline does |
|---|---|
| 201 / 200 with the same entry | `COMPLETED`, `erp_transaction_id` saved and returned |
| Connection refused, 502, 503 | Resend inline (3 attempts, 0.5 s then 1 s), then defer with backoff |
| Read timeout, other 5xx | Look up, then as above |
| 409, 422 | `NEEDS_RECONCILIATION` |
| Entry found with different lines | `NEEDS_RECONCILIATION` |

**The ERP step never goes to `FAILED`.** Once Fineract has moved the money, a missing journal entry means the ledgers disagree. That needs a person, not a "failed" answer to the partner, who would retry with a new reference. The same applies when the mapping refuses a transaction (`FINERACT_DONE -> NEEDS_RECONCILIATION`). The caller gets 202 with the operation status, Fineract's split and the error.

**Retry budget.** `attempts` restarts at 0 when an operation enters `ERP_PENDING`, so the ERP gets the full `operation_max_attempts` (10) whatever Fineract used. Deferred retries use the shared backoff (5 s doubling to 5 min, plus jitter, D-014). After the last one: `NEEDS_RECONCILIATION`.

**Responses.** While the entry is pending: 202, `operation_status: ERP_PENDING` and Fineract's split. When done: 201 (200 on replay) with `erp_transaction_id` (the ERP's `JE-000123`, D-020). Fineract ids are still never returned.

**The ERP returns an id.** `JournalEntryOut` gains `id` (`JE-` plus the zero-padded row id), so the integration service has something to store as `erp_transaction_id`.

**Schema.** `operations.erp_request` is a new JSONB column. Tables come from `create_all`, which does not add columns to an existing table: a stack created before this change needs `make reset` (or `ALTER TABLE operations ADD COLUMN erp_request JSONB`). The stack starts empty (rule 13), so a fresh one is always right.

**Evidence.** `make simulate-loan-lifecycle` (S-D1) now checks the ERP entry for the disbursement and every repayment against Fineract's own transaction, that the nine rejected disbursements have no entry, and that the ERP balances of 1100, 1200 and 4100 moved by exactly Fineract's amounts. The failure scenarios (ERP down, timeouts, crash) belong to Part J.

**Known weaknesses.**
- `NEEDS_RECONCILIATION` has no way out of its own. Reconciliation (D-024) reports the gap and can repair a missing ERP entry, but the operation row stays terminal.
- `erp_request` is a snapshot. If Fineract later reprocesses the loan and changes the split, the ERP entry stays as posted (D-021, reversals).
- The ERP lookup compares the whole request, description included. Changing the description format would make old pending entries look different; with immutable entries that is the safe direction.

## D-023: Failure handling, retry policy and failure injection (Part J)

**State machine.** One table in `domain/states.py`; `check_transition` raises on anything else, inside the row lock that writes the status (D-014).

| From | To | When |
|---|---|---|
| `RECEIVED` | `FINERACT_PENDING` | `prepare` passed (our own checks, live Fineract reads) |
| `RECEIVED` | `FAILED` | `prepare` refused it, or Fineract stayed unreachable on every attempt (nothing was sent) |
| `FINERACT_PENDING` | `FINERACT_DONE` | Fineract's result is known: from the call, or from a lookup by `externalId` |
| `FINERACT_PENDING` | `FAILED` | Fineract refused it (4xx), and a lookup shows nothing was created |
| `FINERACT_PENDING` | `NEEDS_RECONCILIATION` | Fineract's outcome stayed unknown after every attempt |
| `FINERACT_DONE` | `ERP_PENDING` | Money moved; the journal entry is saved in the same write as `FINERACT_DONE` |
| `FINERACT_DONE` | `NEEDS_RECONCILIATION` | Money moved, but no journal entry can be built from Fineract's transaction |
| `FINERACT_DONE` | `COMPLETED` | Only operations that move no money (client, application, approval, GL sync) |
| `ERP_PENDING` | `COMPLETED` | The ERP holds the entry (posted, or found by reference) |
| `ERP_PENDING` | `NEEDS_RECONCILIATION` | The ERP refused it, holds a different entry under the reference, or stayed unreachable on every attempt |

`COMPLETED`, `FAILED` and `NEEDS_RECONCILIATION` have no way out. `FAILED` always means "nothing happened downstream", so the partner may retry with a new reference. A deferred retry keeps its status and only moves `next_attempt_at`.

**Retry policy.**
- **Inline:** up to `operation_inline_attempts` (3) per request, sleeping 0.5 s then 1 s. Used for Fineract and ERP calls alike.
- **Deferred:** when the inline attempts are used up, the row is released with `next_attempt_at` = 5 s doubling to a 5 min cap, plus up to 1 s of jitter (`domain/retry.py`). The caller gets 202 with the current status.
- **Max attempts:** `operation_max_attempts` (10) deferred attempts per stage. The counter restarts when the operation enters `ERP_PENDING` (D-022). After the last one: `FAILED` if nothing was sent, otherwise a final lookup and then `FINERACT_DONE`, `FAILED` or `NEEDS_RECONCILIATION`.
- **Who retries:** the request itself (inline), then the recovery sweep (`FOR UPDATE SKIP LOCKED` on due rows, plus a 60 s lease). The sweep runs at startup, every 30 s from an in-process timer, and on `POST /admin/recover`. No broker, no separate worker.

**What is safe to retry.**

| Call | Safe to resend? |
|---|---|
| Any GET (Fineract or ERP) | Always |
| Fineract command, connection refused / 502 / 503 | Yes: it never reached Fineract |
| Fineract command, read timeout / other 5xx | Only after a lookup by `externalId` finds nothing (rule 4). Disbursements and repayments carry our `external_reference` as `externalId`; approval is looked up by loan status |
| ERP post, connection refused / 502 / 503 | Yes |
| ERP post, read timeout / other 5xx | Look up by reference first. The resend is the saved, byte-identical entry, so even a blind resend would get 200 and the same entry (D-020, D-022) |
| Any 4xx (Fineract or ERP) | Never: the answer will not change |
| A partner request with the same `external_reference` and payload | Always: 202 while in progress, the stored result when finished (D-014) |

**Failure injection.** `app/faults.py`, driven by `POST/GET/DELETE /admin/faults`. Off unless `FAULT_INJECTION_ENABLED=true`; the routes then answer 404 and the hooks never fire. Each fault is scoped to one `external_reference` and fires a set number of times, so the recovery timer and other requests are never affected.

| Fault | Hook | What it stands for |
|---|---|---|
| `FINERACT_DOWN` | Before `prepare` | Fineract unreachable: nothing sent |
| `FINERACT_TIMEOUT_AFTER_SEND` | After `execute` returned (the command plus its read-back of the new transaction) | Fineract booked it; the response was lost. The pipeline discards the answer and must find out again |
| `ERP_TIMEOUT_AFTER_WRITE` | After the ERP post returned | The ERP wrote the entry; the response was lost |
| `CRASH_BEFORE_ERP` | After `ERP_PENDING` is saved, before the ERP call | `os._exit(1)`: the process dies |

- "ERP down" needs no hook: the scenario really stops the `mock-erp` container.
- "Fineract down" is injected rather than real: Fineract takes minutes to start again, and stopping it would also break the other scenarios' reads. The hook raises the same `FineractUnavailableError` the client raises on a refused connection, before `prepare`, so the whole path from there on is the real one.
- The crash is real: the process exits, Docker restarts it (`restart: unless-stopped`), and recovery runs in the new process.
- Faults live in memory on purpose. They are test switches, not business state, and a crash must clear them.

**Evidence.** `make simulate-failures` (S-J1 to S-J5) runs all five scenarios on one loan and prints, for each, our operation, Fineract's transactions with that `externalId`, and the ERP entry plus a row count from the ERP database, before, during and after recovery.

**Known weaknesses.**
- With fault injection on, anyone holding the admin key can crash the service. `/admin/*` needs the admin key (D-027), and faults are for this assessment's local stack only; a real deployment would never enable them.
- After a crash, the operation waits for its lease (60 s) before the sweep takes it. A shorter lease recovers faster but risks overlapping a slow request (D-014).
- `NEEDS_RECONCILIATION` operations stay terminal; D-024 reports and repairs the ERP gap behind them.

## D-024: Reconciliation (Part K)

**Decision.** `POST /api/reconciliation` compares Fineract's disbursements and repayments with the ERP's journal entries, keyed by `external_reference`, and returns one row per expected or found journal entry. The comparison is a pure function (`domain/reconciliation.py`, `tests/test_reconciliation.py`); `services/reconciliation.py` only gathers the two sides.

**Why `POST` for a read.** The brief names `POST /api/reconciliation`, and reference mode sends up to 500 references, which belong in a body rather than a query string. The call changes nothing, so it takes no `external_reference` and writes no operation row. Repairs, which do change state, have their own route.

**Request.** Exactly one of `from_date` + `to_date`, or `external_references` (1 to 500). Anything else gets 422. If Fineract or the ERP can't be reached, the call returns 503 and no report: a partial report would show real-looking gaps.

**Gathering the two sides.**
- **Fineract:** every loan in `loan_map` is read live with its transactions (`GET /loans/external-id/{loan_reference}?associations=transactions`). Only disbursements and repayments count; accruals and the rest have no ERP entry (D-021). Date mode keeps transactions dated inside the range. Reference mode keeps the ones whose `externalId` was asked for. Both modes index every transaction of every loan, so a reference whose ERP entry is dated differently is still paired. The operations table is never used as a source of loan data (rule 7). It only adds `operation_status` to a row so a `NEEDS_RECONCILIATION` operation shows next to its gap.
- **ERP:** a new `GET /api/journal-entries?from_date&to_date` lists entries by `entry_date`. Only *loan entries* count: those with a line on 1200, 4100 or 2100. That keeps unrelated journals out of `MISSING_IN_FINERACT`. Any expected entry not in the list is then looked up by reference (`GET /api/journal-entries/{ref}`) before it is called missing. That catches a `-REV` entry dated on the reversal day, outside the range.

**Comparison.** The expected entry is `journal_entry_for(transaction)`, the same function the posting step uses (D-021), so the numbers are Fineract's (rule 5).

| Case | Status |
|---|---|
| Expected lines equal the ERP lines | `MATCHED` |
| Fineract only | `MISSING_IN_ERP` |
| ERP only (loan entry) | `MISSING_IN_FINERACT` |
| Any difference in the total, an account's debit or credit, or the currency | `AMOUNT_MISMATCH`, with `differences` such as `1200 credit: Fineract 186588, ERP 161588` |

- **"Same amount and type".** The brief has four statuses. Type is checked through the accounts: a repayment debits 1100, a disbursement credits it. So a wrong type, or a correct total with a wrong principal/interest split, is `AMOUNT_MISMATCH`, and `differences` says exactly which lines.
- **Not compared:** dates and descriptions. The brief says amount and type, and the date range already decides what is in scope. Both dates are in the response.
- **Edge cases:**
  - A Fineract disbursement or repayment with no `externalId` (booked outside us without one) is `MISSING_IN_ERP` with `external_reference: null` and a note: no ERP entry can carry it.
  - A transaction the mapping refuses (for example with a fee portion) is `MISSING_IN_ERP`, or `AMOUNT_MISMATCH` if the ERP has an entry under its reference, with the mapping error as the note.

**Reversals.** ERP entries are immutable (D-020), so a reversal is a second entry, `<ref>-REV`, with debits and credits swapped and dated on Fineract's `reversedOnDate` (`reversal_of`, D-021). A reversed Fineract transaction (`manuallyReversed: true`) expects both entries, and each is its own row:

| Fineract | ERP | Rows |
|---|---|---|
| Reversed | Original and `-REV` | Both `MATCHED` (net zero on both sides) |
| Reversed | Original only | Original `MATCHED`, `-REV` `MISSING_IN_ERP` |
| Reversed | Neither | One `MATCHED` row with a note: reversed before it reached the ERP, both sides net to zero |
| Not reversed | Original and `-REV` | Original `MATCHED`, `-REV` `MISSING_IN_FINERACT` |

If Fineract reprocesses later repayments after a reversal and changes their split, those repayments show as `AMOUNT_MISMATCH` against the entries posted earlier. That is correct: the ERP holds a snapshot (D-022). The service still has no undo route (D-017). The simulation reverses a transaction directly in Fineract (`POST /loans/external-id/{loan}/transactions/external-id/{ref}` with `transactionAmount: 0`, the 1.16.1 adjust command).

**Repair: explicit and idempotent.** `POST /api/reconciliation/repairs {external_reference, loan_reference, transaction_reference}` is the only way the reconciliation writes anything.
- **What it repairs:** only `MISSING_IN_ERP`. It reads the Fineract transaction, builds the entry (or the `-REV` reversal), and posts it to the ERP.
- **How it runs:** it is a new operation type, `REPAIR_ERP_POSTING`, run by the normal pipeline: the Fineract step is a read, followed by `FINERACT_DONE -> ERP_PENDING -> COMPLETED`. So it gets the operations table's idempotency, the same retries and recovery, and look-up-before-resend.
- **Idempotent twice over:**
  - Same reference and payload: the stored result. Same reference, other payload: 409.
  - A second repair reference for the same transaction finds the identical entry already in the ERP and returns its `erp_transaction_id`, because the ERP is unique on `external_reference`.
- **Refused:**
  - The ERP already holds a *different* entry under the reference (`AMOUNT_MISMATCH`): 409. ERP entries are immutable, and deciding which side is wrong needs a person.
  - The transaction is not in Fineract (`MISSING_IN_FINERACT`): 404. Repair never writes to Fineract, the source of truth for loans.
  - A `-REV` for a transaction that isn't reversed: 409.
- **What it doesn't change:** a repair does not move an older operation out of `NEEDS_RECONCILIATION`, which stays terminal (D-023). After a repair, the report shows the row `MATCHED` with `operation_status: NEEDS_RECONCILIATION`, which keeps the history visible.

**Evidence.** `make simulate-reconciliation` (S-K1 to S-K3): all four outcomes plus a reversal seeded on one loan; both report modes; read-only checked by row counts; repair, replay and the refusals.

**Known weaknesses.**
- The Fineract side reads every loan in `loan_map`, one call per loan, on every run. That's fine at assessment scale. At volume it needs Fineract-side filtering (for example the journal entries API by date) or paging.
- Loans created in Fineract outside this service are not scanned, so their transactions can't be `MISSING_IN_ERP`. Their ERP entries still show as `MISSING_IN_FINERACT`.
- "Loan entry" is decided by account (1200, 4100, 2100). A manual ERP journal on those accounts that has nothing to do with a loan shows as `MISSING_IN_FINERACT`. A `source` field on ERP entries would be more exact, but it changes the ERP schema.
- Date mode selects Fineract transactions by their transaction date. A transaction dated before the range but reversed inside it is not picked up by that range.
- Reports are not stored. Each call recomputes from the live systems.
- GL (master-data) discrepancies are reconciled separately (D-025).

## D-025: GL master-data reconciliation (Part L, stretch)

**Decision.** `GET /api/reconciliation/gl-accounts` compares Fineract's chart of accounts with the ERP's and returns one row per account found on either side. Same layout as D-024: the comparison is a pure function (`domain/gl_reconciliation.py`, `tests/test_gl_reconciliation.py`), and `services/gl_reconciliation.py` only gathers the two sides.

**Why `GET`.** It takes no input and changes nothing, so it writes no operation row and takes no `external_reference`. Part K uses `POST` only because the brief names it and reference mode needs a body (D-024). If Fineract or the ERP can't be reached: 503 and no partial report.

**Gathering the two sides.**
- **ERP:** `GET /api/accounts`, the whole chart.
- **Fineract:** the stock `GET /glaccounts`. Patch 0001 adds `externalReference` to the shared row mapper (D-007), so the stock list already returns it. No new endpoint. A JUnit integration test in the patch proves the list returns the reference for a synced account and `null` for a stock one, because this report depends on it. The least-privilege user already has `READ_GLACCOUNT` (D-013).
- **Header accounts are skipped.** Sync only creates detail accounts (D-008), and the ERP has no header concept.
- **`sync_status`** from `gl_account_sync` is added to each row, the same way D-024 adds `operation_status`. It is context, not a source: the comparison never reads it.

**Pairing.** By the ERP `external_reference` that sync stores on the Fineract account. If an ERP account has no linked Fineract account, its code is checked against every Fineract account, so a clash is named rather than reported as two unrelated gaps.

| Case | Status |
|---|---|
| Same reference; code, name and type agree | `MATCHED` |
| ERP account, no Fineract account with its reference or code | `MISSING_IN_FINERACT` |
| Fineract account with no reference (created outside the sync), or with a reference the ERP does not hold | `MISSING_IN_ERP` |
| ERP account whose code is already used by a Fineract account with another or no reference | `CODE_CONFLICT`: one row with both sides. Sync answers 409 for it and never adopts an existing account (D-007) |
| Same reference, but code, name or type differ | `DETAILS_MISMATCH`, with `differences` such as `name: ERP 'Cash at Bank', Fineract 'Cash'` |

- **What is compared:** code, name and type, the fields sync writes. The type is compared through `ERP_TO_FINERACT_GL_TYPE`, the same table sync uses (D-008), so the two can't drift.
- **Not compared:** usage, disabled and manual-entries flags, and the description. The ERP has no such fields.
- **Mapping errors are reported, not hidden.** If `to_fineract_gl_account` would refuse an ERP account (wrong currency, unknown type, name over 45 characters), the row carries that error as its `note`. A `MISSING_IN_FINERACT` row then says why sync will never create it.
- **No Fineract numeric ids** in the response (rule 6). Rows are keyed by `external_reference` and `gl_code`.

**No repair.** Sync is create-only (rule 12), and Fineract is never changed from a report. `MISSING_IN_FINERACT` is fixed by the normal `POST /api/gl-accounts/sync`. Every other status needs a person: deciding whether the ERP or the back office renamed an account, or which of two accounts owns a code, is a business decision.

**Evidence.** `make simulate-gl-reconciliation` (S-L1 to S-L3): one account per outcome, the report twice with row counts before and after, then a sync and the report again.

**Known weaknesses.**
- The report covers the whole chart. Leftovers from earlier simulation runs, or anything a back office added, appear in the summary. There is no filter by reference or code.
- Both sides are read in full on every call. That's fine for a chart of accounts, which is small and changes rarely.
- Fineract's stock `PUT /glaccounts/{id}` can still rename or recode a synced account. The patch does not block it (outside rule 11's scope); this report is how it is detected.
- Reports are not stored, and nothing runs on a schedule. Each call reads both systems live.

## D-026: Duplicates and concurrency (Part I)

**What stops a second disbursement.** No single layer is enough on its own, so there are five. Each one covers a gap the layer above leaves. Evidence: `make simulate-concurrency`, SIMULATIONS.md S-I1 to S-I3.

| # | Layer | Where | What it stops | Why it is not enough alone |
|---|---|---|---|---|
| 1 | API idempotency | `operations` unique `(operation_type, external_reference)` + payload hash (D-014) | The same request twice, sequential or concurrent: one `INSERT` wins, the rest replay (200) or see it in progress (202) | Keyed by reference: two different references for one loan both pass |
| 2 | Integration-DB constraint | `loan_disbursement` primary key `loan_reference`: one slot per loan (D-017) | Different references disbursing one loan: the second request finds the slot taken and gets 409 before Fineract is called | Only guards calls made through this service, and only while our database is right |
| 3 | Row lock | `SELECT ... FOR UPDATE` on `loan_map` while claiming the slot | Two requests both reading a free or `FAILED` slot and both taking it over | Same scope as layer 2 |
| 4 | Fineract `externalId` | Our `external_reference` is the transaction's `externalId`; unique index `m_loan_transaction_external_id_key` | A resend of one reference booking twice in Fineract (e.g. after a timeout) | Keyed by reference again: a different reference is a different `externalId` |
| 5 | Fineract loan state | `LoanTransactionValidatorImpl` rejects disbursing a single-disbursement loan that is not "approved and not disbursed" | Any second disbursement of the loan, whatever its reference or origin | Gives the caller an error, not a replay. It's a backstop, not the contract |

**The last line of defence is layer 5, Fineract's loan state.** It is the only check that knows whether money went out. It holds even if our database is wrong, restored from a backup, or bypassed, and even if a call comes from somewhere other than this service. Layers 1 to 3 exist so that this backstop is never needed in normal use, and so callers get a clean replay or 409 instead of a Fineract validation error.

**Repayments use layers 1 and 4 only.** Many legitimate repayments can hit one loan, so there is no per-loan slot. Concurrent repayments with different references therefore all reach Fineract at once. Fineract serialises them itself with optimistic locking on the loan and retries the losing command up to 3 times (`fineract.retry.instances.executeCommand`).

**Fineract's concurrency 409 is retryable, not a rejection.** When Fineract's own retries run out, it rolls the command back and returns 409 with code `error.msg.platform.service.<type>.conflict` ("unable to handle the request due to concurrent modification, please try again"). `error.msg.loan.locked` (loan locked by the close-of-business job) has the same meaning. `is_concurrent_modification` in `clients/fineract.py` recognises both, and the client raises `FineractUnavailableError` for them, so the pipeline retries inline with backoff and then defers to the recovery sweep. Every other 4xx stays a `FineractRejectedError`; a business 409 such as `error.msg.glaccount.glcode.conflict` is still a rejection (`tests/test_fineract_errors.py`). Before this change the first run of the I3 simulation failed: 6 of 10 concurrent repayments ended `FAILED` with 422. Nothing was double-booked; they were simply lost.

**Why resending after that 409 needs no lookup first.** Fineract answered, so the outcome is known: the command was rolled back. This matches how `FineractUnavailableError` is already handled, where the request never took effect (`test_unavailable_is_resent_without_look_up`). If that assumption were ever wrong, layer 4 would reject the resend as a duplicate `externalId`, and the pipeline's rejection path looks the reference up before failing (D-023).

**What the simulation counts.** "Integration transactions" in the brief's I1 table is the number of `COMPLETED` operations for the loan. The script also prints every row by status. In I2b the 9 losers are stored as `FAILED` operations with a 409 response, so a repeat of a losing reference replays its 409. They never reached Fineract.

**Known weaknesses.**
- Fineract can still return a concurrency 409 on every inline and deferred attempt under sustained contention on one loan. The operation then ends `FAILED` (Fineract never applied it) after `operation_max_attempts`, and the partner must resend.
- The overpayment race in D-017 remains: concurrent repayments that each pass the live balance check can together exceed the balance. The simulation's repayments total 1,000,000 of 5,500,799 outstanding, so they don't exercise it.
- Retried repayments are applied in whatever order Fineract books them, so the principal/interest split of each one depends on that order (S-I3). The totals are what Fineract's schedule says either way, and the ERP mirrors each transaction's own split.

## D-027: API keys and one error format (Part E)

**Authentication.** Every route under `/api` needs the header `X-API-Key` matching `PARTNER_API_KEY`. Every route under `/admin` needs `ADMIN_API_KEY`. `/health`, `/docs` and `/openapi.json` stay open. A missing or wrong key gets 401 with `WWW-Authenticate: X-API-Key`, before the request body is validated or any operation row is written.
- **Why an API key, not OAuth2 or mTLS.** The partner is a single server-to-server caller, and the brief asks for authentication, not identity management. A static key carried over TLS is the usual contract for that. OAuth2 client credentials would add a token issuer to the stack for no extra correctness.
- **Why two keys.** `/admin` can run the recovery sweep and, with fault injection on, crash the service (D-023). A partner key must not be able to do that.
- **How it is checked.** `app/auth.py` compares with `hmac.compare_digest`, so timing doesn't leak how much of a key matched. FastAPI's `APIKeyHeader` scheme makes `/docs` show an "Authorize" button for each key. Both keys are required settings, so the service won't start without them. Compose reads them from `.env` (`:?set in .env`).
- **Scripts and Postman.** `scripts/integration_auth.py` adds the right key by path, so every simulation and `bootstrap.py` authenticate. The Postman folder uses `{{partner_api_key}}`, and the recovery request uses `{{admin_api_key}}`.

**Error format.** Every error response is one flat JSON object:

```json
{"error": "loan LN-2026-999999 not found", "code": "not_found"}
```

- `error` is the human message, and `code` is a stable machine code taken from the status (`app/errors.py`): `unauthorized`, `not_found`, `conflict`, `unprocessable`, `service_unavailable`, `upstream_error`, `internal_error`. Request validation uses `invalid_request` and adds `details`, a list of `{"in", "field", "message"}` naming the body, path or query field.
- Operation failures keep their context next to `error` and `code`: `external_reference`, `loan_reference`, `operation_status`. One shape covers a live failure and the replay of a stored one.
- **No Fineract text reaches a partner.** Fineract's request paths (`/loans/7/...`) and some of its messages ("Loan: 7 disbursement is not allowed on charged-off loan.") carry its numeric IDs (rule 6). So a failure caused by Fineract gets a fixed message: "Fineract was unavailable on every attempt", "Fineract did not accept the request after every attempt", or "Fineract rejected the request: <code>". Fineract's error code (e.g. `error.msg.loan.charged.off`) is a fixed string with no IDs. The full detail (transport error, Fineract's message) goes to `operations.last_error`, which only `/admin/operations/{ref}` shows. `tests/test_pipeline.py` covers each path with an error that contains an ID and checks the partner body doesn't. Before this rule, the transport error was copied into the partner's body. No run reached that path, and a scan of the 72 stored operations found no Fineract ID.
- **Why flat, not `{"error": {...}}`.** Operation results were already flat with an `error` string, and their replay bodies are stored in `operations.response`. Keeping them flat means a replay and a live error look the same.
- **Where it's enforced.** Handlers for `HTTPException` and `RequestValidationError` cover every route, including 404 and 405 from routing. An unhandled exception is caught in the correlation-id middleware, logged with its stack trace, and answered as `500 internal_error`. Catching it there means even a 500 carries `X-Correlation-ID`.
- **Tests.** `tests/test_errors.py` covers the pure functions. `tests/test_api_contract.py` drives the app over HTTP without its lifespan (no database or downstream calls): 401 for no key, a wrong key, or the other route group's key; open `/health` and `/openapi.json`; body and path validation; `HTTPException`; an unhandled error; and correlation ids.

**Known weaknesses.**
- One key per route group and no per-partner identity. A second partner would need its own key and an `operations` column recording which partner sent each request. Keys rotate only by restarting with new values.
- The service itself serves plain HTTP on port 8000, so in a real deployment TLS would come from a reverse proxy in front of it.
- Failed operations stored before this change replay their original body, which has no `code`.

## D-028: `GET /api/transactions/{external_reference}` (Part E)

**Decision.** A partner can read any request it has sent by its `external_reference`: GL sync, client creation, loan application, approval, disbursement, repayment or ERP repair. The response is the operation row, never a live call to Fineract or the ERP:

```json
{"external_reference": "SIM-450991-REPAY-1", "operation_type": "REPAY_LOAN", "operation_status": "COMPLETED", "attempts": 0,
 "created_at": "...", "updated_at": "...",
 "result": {"loan_reference": "LN-2026-000001", "amount": 500000, "principal_portion": 383400, "interest_portion": 116600,
            "outstanding_after": 4616600, "erp_transaction_id": "JE-000002", "operation_status": "COMPLETED", ...}}
```

- **`result`** is the body the partner got (or will get on replay), with the current `operation_status` on top. For a finished operation it equals the stored response, which the lifecycle simulation checks against the original POST. For one still in progress, it carries what is known so far, e.g. Fineract's split while the ERP posting is pending. A failed operation shows its `error` and `code`.
- **Read from our table, not Fineract.** The brief calls this a transaction lookup, and the operations table is the record of what this service did and what it answered. Loan status and balances stay live in `GET /api/loans/{loan_reference}` (rule 7). The principal/interest split in `result` is Fineract's own, as recorded when the transaction was booked (D-021).
- **Not exposed:** the raw `last_error`, `next_attempt_at` and `locked_until`. They are internal retry state, and a transport error's text can contain a Fineract URL with a numeric ID. `/admin/operations/{ref}` still shows them to operators.
- **One reference, several types.** Uniqueness is per `(operation_type, external_reference)` (D-014), so a partner can legally reuse a reference across types. Then the lookup returns 409 `ambiguous_reference` listing the types, and `?operation_type=REPAY_LOAN` picks one. No reference is guessed.
- **404** (`not_found`) for an unknown reference. It needs the partner key (D-027).
- **Evidence.** `tests/test_transactions.py` covers the view and the route (found, in progress, 404, ambiguous, filter, bad filter, auth). Step 11 of `make simulate-loan-lifecycle` reads a repayment, the payoff (equal to its POST body), a rejected disbursement and an unknown reference. Step 12 scans these responses for Fineract IDs too.

**Known weaknesses.**
- No list or search by loan; `GET /api/loans/{loan_reference}` lists a loan's transactions live from Fineract.


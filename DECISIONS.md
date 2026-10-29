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
- The integration service uses Fineract's stock admin user until bootstrap creates a least-privilege one.
- TLS verification to Fineract is off, because Fineract's image ships a self-signed certificate. Dev only.
- `FAILED` is final. Fixing the cause and retrying needs a manual step, which isn't built.

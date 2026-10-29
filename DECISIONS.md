# DECISIONS

## D-001: Fineract 1.16.1

Pinned to tag `1.16.1` (commit `c643dab3`), the latest release when work started (2026-10-28). The build also checks the commit hash, so a re-pointed tag fails the build instead of silently changing the source.

Consequence: the tag's Gradle toolchain requires JDK 25 and Gradle 9.7.1 (wrapper).

## D-002: Build Fineract inside Docker

Fineract is compiled in a multi-stage Docker build on `eclipse-temurin:25.0.4.1_1-jdk`, so the stock and patched builds use the same toolchain on any machine. A local JDK 25 is still needed for `make test-java` and for work in the Fineract clone (which lives outside this repository).

## D-003: One PostgreSQL server, one database per service

PostgreSQL 18.3 (the version used by the 1.16.1 tag's own compose files; first pinned as 16.15, changed before any data existed) hosts `fineract_tenants`, `fineract_default`, `erp` and `integration` as separate databases. Fineract 1.16.1 supports PostgreSQL, and the integration service's recovery sweep relies on `FOR UPDATE SKIP LOCKED`. Services never read each other's databases.

Known weakness: one server is a shared failure point. Acceptable for an assessment stack.

## D-004: Fineract image build settings

- Only `:fineract-provider:bootJar` is built, skipping tests (`-x test -x cucumber`). `buildJavaSdk` is deliberately not skipped: it generates the Avro classes `fineract-core` needs. Upstream's Docker CI skips it only because it reuses a pre-built workspace; copying that exclusion broke our first clean build. Fineract's own test suite is not run in the image build; our patch's tests run separately via `make test-java`.
- The tag's `gradle.properties` requests a 12 GB Gradle heap. The build overrides only the heap (`GRADLE_XMX`, default 4g) and caps workers at 2, so it fits an 8 GB Docker Desktop. The other JVM flags from the tag are kept unchanged.
- The image runs the jar with `java -jar` on Temurin 25 JRE as user `nobody`, with the same JVM flags as the tag's Jib config. We do not use Jib, because the Dockerfile must apply our patches between clone and build.

## D-005: How patches reach the image

- The Java change is committed in a Fineract clone outside this repo and exported with `git format-patch <tag>` into `fineract-customization/patch/`. Only those files are committed here.
- The Dockerfile applies them with `git apply --index`, not `git am`: `git am` needs a committer identity inside the container and adds nothing to the image. Consequence: the jar's `git.properties` still reports the tag's commit, so the image is identified by tag + patch files, not by a commit hash of our own.
- Patches apply in file-name order and the build fails on the first one that does not apply cleanly, so an image can never silently lack part of the change.
- `make build-fineract` regenerates the patches from the clone when one exists (`FINERACT_SRC`, default `../fineract`) and otherwise uses the committed ones, so graders need no clone. `--no-signature` keeps regenerated files stable across git versions.

## D-006: Compose stack configuration

- Fineract settings are adapted from the tag's `config/docker/env/*.env` into `fineract/configuration/fineract.env`. Removed: the remote debugger (`-agentlib:jdwp` on port 5000), the `test` Spring profile (it enables internal test-only APIs such as `InternalClientInformationApiResource` and `InternalLoanInformationApiResource`), the `diagnostics` profile, the Prometheus endpoint, and the MySQL-only Hikari datasource properties. Management endpoints are limited to `health,info`.
- Tenant timezone is `Africa/Kigali` (upstream's demo uses `Asia/Kolkata`). It sets Fineract's business date, which loan transaction dates must respect.
- Each service gets its own database login and owns only its own database(s): `fineract` (fineract_tenants, fineract_default), `erp`, `integration`. Passwords live in a git-ignored `.env`; `.env.example` is committed.
- Postgres is published on host port 5433 (`DB_HOST_PORT`) so it does not clash with a local postgres on 5432. Services use `db:5432` internally.
- `integration-service` starts only after Fineract and the ERP are healthy, since it is the only service that calls them.
- `make up` uses `docker compose up --wait` with a 15-minute timeout because Fineract's first boot runs every Liquibase changeset.

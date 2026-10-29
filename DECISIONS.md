# DECISIONS

## D-001: Fineract 1.16.1

Pinned to tag `1.16.1` (commit `c643dab3`), the latest release when work started (2026-10-28). The build also checks the commit hash, so a re-pointed tag fails the build instead of silently changing the source.

Consequence: the tag's Gradle toolchain requires JDK 25 and Gradle 9.7.1 (wrapper).

## D-002: Build Fineract inside Docker

Fineract is compiled in a multi-stage Docker build on `eclipse-temurin:25.0.4.1_1-jdk`, so the stock and patched builds use the same toolchain on any machine. A local JDK 25 is still needed for `make test-java` and for work in the Fineract clone (which lives outside this repository).

## D-003: One PostgreSQL server, one database per service

PostgreSQL 16.15 hosts `fineract_tenants`, `fineract_default`, `erp` and `integration` as separate databases. Fineract 1.16.1 supports PostgreSQL, and the integration service's recovery sweep relies on `FOR UPDATE SKIP LOCKED`. Services never read each other's databases.

Known weakness: one server is a shared failure point. Acceptable for an assessment stack.

## D-004: Fineract image build settings

- Only `:fineract-provider:bootJar` is built, skipping tests (`-x test -x cucumber`). `buildJavaSdk` is deliberately not skipped: it generates the Avro classes `fineract-core` needs. Upstream's Docker CI skips it only because it reuses a pre-built workspace; copying that exclusion broke our first clean build. Fineract's own test suite is not run in the image build; our patch's tests run separately via `make test-java`.
- The tag's `gradle.properties` requests a 12 GB Gradle heap. The build overrides only the heap (`GRADLE_XMX`, default 4g) and caps workers at 2, so it fits an 8 GB Docker Desktop. The other JVM flags from the tag are kept unchanged.
- The image runs the jar with `java -jar` on Temurin 25 JRE as user `nobody`, with the same JVM flags as the tag's Jib config. We do not use Jib, because the Dockerfile must apply our patches between clone and build.

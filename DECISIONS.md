# DECISIONS

## D-001: Fineract 1.16.1

Pinned to tag `1.16.1` (commit `c643dab3`), the latest release when work started (2026-10-28). The build also checks the commit hash, so a re-pointed tag fails the build instead of silently changing the source.

Consequence: the tag's Gradle toolchain requires JDK 25 and Gradle 9.7.1 (wrapper).

## D-002: Build Fineract inside Docker

Fineract is compiled in a multi-stage Docker build on `eclipse-temurin:25.0.4.1_1-jdk`, so the stock and patched builds use the same toolchain on any machine. A local JDK 25 is still needed for `make test-java` and for work in the Fineract clone (which lives outside this repository).

## D-003: One PostgreSQL server, one database per service

PostgreSQL 16.15 hosts `fineract_tenants`, `fineract_default`, `erp` and `integration` as separate databases. Fineract 1.16.1 supports PostgreSQL, and the integration service's recovery sweep relies on `FOR UPDATE SKIP LOCKED`. Services never read each other's databases.

Known weakness: one server is a shared failure point. Acceptable for an assessment stack.

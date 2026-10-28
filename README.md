# Fineract + ERP integration assessment

Apache Fineract (customized, built from source), a Mock ERP and an Integration Service, run together with Docker Compose.

## Versions

Every version below is pinned. Do not upgrade them without recording the change in [DECISIONS.md](DECISIONS.md).

| Component | Version | Where it is pinned |
|---|---|---|
| Apache Fineract | tag `1.16.1`, commit `c643dab306fe29798951591ce825fd5e5272923e` | `FINERACT_TAG` / `FINERACT_COMMIT` in `fineract-customization/Dockerfile` |
| JDK (Fineract build and runtime) | Eclipse Temurin 25 (`eclipse-temurin:25.0.4.1_1-jdk` / `-jre`) | `fineract-customization/Dockerfile`; required by the tag's Gradle toolchain (`JavaLanguageVersion.of(25)`) |
| Gradle | 9.7.1, via the Fineract wrapper (`./gradlew`) | the tag's `gradle/wrapper/gradle-wrapper.properties`; never use a system Gradle |
| PostgreSQL | 16.15 (`postgres:16.15`) | `docker-compose.yml` |
| Python (Mock ERP, Integration Service) | 3.12.16 (`python:3.12.16-slim`) | service Dockerfiles |

### Host prerequisites

| Tool | Minimum | Tested with |
|---|---|---|
| Docker Engine | 24 | 27.4.0 |
| Docker Compose | v2 | v2.31.0 |
| Docker memory | 8 GB (10–12 GB recommended for the Fineract build) | 8 GB |
| JDK, only for `make test-java` and work in the Fineract clone | Temurin 25 | — |

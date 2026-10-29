# Fineract + ERP integration assessment

Apache Fineract (customized, built from source), a Mock ERP and an Integration Service, run together with Docker Compose.

## Versions

Every version below is pinned. Do not upgrade them without recording the change in [DECISIONS.md](DECISIONS.md).

| Component | Version | Where it is pinned |
|---|---|---|
| Apache Fineract | tag `1.16.1`, commit `c643dab306fe29798951591ce825fd5e5272923e` | `FINERACT_TAG` / `FINERACT_COMMIT` in `fineract-customization/Dockerfile` |
| JDK (Fineract build and runtime) | Eclipse Temurin 25 (`eclipse-temurin:25.0.4.1_1-jdk` / `-jre`) | `fineract-customization/Dockerfile`; required by the tag's Gradle toolchain (`JavaLanguageVersion.of(25)`) |
| Gradle | 9.7.1, via the Fineract wrapper (`./gradlew`) | the tag's `gradle/wrapper/gradle-wrapper.properties`; never use a system Gradle |
| PostgreSQL | 18.3 (`postgres:18.3`, the version the tag's own compose files use) | `docker-compose.yml` |
| Python (Mock ERP, Integration Service) | 3.12.16 (`python:3.12.16-slim`); dependencies locked with uv 0.9.8 (`uv.lock`) | service Dockerfiles, `pyproject.toml`, `uv.lock` |

### Host prerequisites

| Tool | Minimum | Tested with |
|---|---|---|
| Docker Engine | 24 | 27.4.0 |
| Docker Compose | v2 | v2.31.0 |
| Docker memory | 8 GB (10–12 GB recommended for the Fineract build) | 8 GB |
| JDK, only for `make test-java` and work in the Fineract clone | Temurin 25 | — |

## Running the stack

```bash
cp .env.example .env      # then set real passwords
make up                   # builds Fineract from source (first time ~10 min), starts all services, waits until healthy
```

| Service | URL |
|---|---|
| Fineract | `https://localhost:8443/fineract-provider` (self-signed certificate; tenant header `Fineract-Platform-TenantId: default`) |
| Integration Service | `http://localhost:8000` |
| Mock ERP | `http://localhost:8001` |
| PostgreSQL | `localhost:5433` (`DB_HOST_PORT`) |

`make down` stops the stack; `make reset` also deletes the database volume, so the next `make up` starts empty. `make logs` and `make ps` show logs and status.

Plain `docker compose up --build` also works and builds Fineract from source through the `build:` section in `docker-compose.yml`.

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
| Python 3 on the host, for `make bootstrap` and the `make simulate-*` / `demo-*` / `evidence-*` scripts (standard library only) | 3.11 (macOS's stock `/usr/bin/python3` 3.9 is too old) | 3.11.0 |
| uv, for `make test-python` and `make lint` (it installs the services' Python 3.12 and locked dependencies itself) | 0.9.8 | 0.9.8 |
| JDK, only for `make test-java` and work in the Fineract clone | Temurin 25 | — |

## Running the stack

```bash
cp .env.example .env      # then set real passwords and the two API keys (PARTNER_API_KEY, ADMIN_API_KEY)
make up                   # builds Fineract from source (first time ~15 min), starts all services, waits until healthy
make bootstrap            # configures Fineract via its REST API: office, RWF, API user, GL accounts, loan product
```

`make bootstrap` is safe to run again; a second run changes nothing (DECISIONS.md D-009).

| Service | URL |
|---|---|
| Fineract | `https://localhost:8443/fineract-provider` (self-signed certificate; tenant header `Fineract-Platform-TenantId: default`) |
| Integration Service | `http://localhost:8000` |
| Mock ERP | `http://localhost:8001` |
| PostgreSQL | `localhost:5433` (`DB_HOST_PORT`) |

`make down` stops the stack; `make reset` also deletes the database volume, so the next `make up` starts empty. `make logs` and `make ps` show logs and status.

Plain `docker compose up --build` also works and builds Fineract from source through the `build:` section in `docker-compose.yml`.

Every call to the integration service needs `X-API-Key: $PARTNER_API_KEY` (`/api/*`) or `$ADMIN_API_KEY` (`/admin/*`). OpenAPI docs: `http://localhost:8000/docs`. A Postman collection is in `postman/collection.json`.

## Tiers completed

| Tier | Parts | Status |
|---|---|---|
| 1. Core | A–G | Done |
| 2. Reliability | H–K | Done |
| 3. Stretch | L | One item: GL master-data reconciliation (`GET /api/reconciliation/gl-accounts`, D-025) |

Known weaknesses are listed in each decision in [DECISIONS.md](DECISIONS.md); the summary at its top collects the main ones.

## Tests

| Language | Command | What it runs | Evidence |
|---|---|---|---|
| Java | `make test-java` | The patch's JUnit tests in the Fineract clone: 15 unit tests (`fineract-accounting`) and 5 integration tests against the running stack. Needs the clone at `fineract-src/` and a local Temurin 25 | `evidence/test-java.log` |
| Python | `make test-python` | pytest for the Mock ERP and the Integration Service: unit tests (mappings, state machine, idempotency, retry, reconciliation) and HTTP contract tests (auth, errors, validation, transactions) | `evidence/test-python.log` |
| Python | `make lint` | ruff (lint and format) and mypy (strict type checking) on both services | `evidence/lint.log` |

**Integration-level tests** (the integration service against the real Fineract and the real Mock ERP, plus failure, duplicate and concurrency tests) are the `scripts/simulate_*.py` scripts. They run against the running stack, assert on each system's state and exit non-zero on any failed check:

| Command | Covers | Evidence |
|---|---|---|
| `make evidence-bootstrap` | Fineract setup via REST, twice (get-or-create), then the setup checks | `bootstrap-run1.log`, `bootstrap-run2.log`, `verify-fineract-setup.log` |
| `make demo-fineract-gl` | The Java customization's behaviours 1–4, straight against Fineract | `demo-fineract-gl-sync.log` |
| `make simulate-gl-sync` | Same ERP account sent 4 times | `simulate-gl-sync.log` |
| `make simulate-loan-lifecycle` | One loan from application to Closed, with ERP posting | `simulate-loan-lifecycle.log` |
| `make simulate-erp` | Mock ERP rules | `simulate-erp.log` |
| `make simulate-concurrency` | Part I: duplicates, 10 parallel disbursements, 30 parallel repayments | `simulate-concurrency.log` |
| `make simulate-failures` | Part J: the five failure scenarios (needs `FAULT_INJECTION_ENABLED=true`) | `simulate-failures.log` |
| `make simulate-reconciliation` | Part K: all four outcomes, a reversal, repairs | `simulate-reconciliation.log` |
| `make simulate-gl-reconciliation` | Part L | `simulate-gl-reconciliation.log` |
| `make evidence-observability` | "What happened to FIN-2026-000001?" from one API call and the logs | `observability.log` |

Each run is written up in [SIMULATIONS.md](SIMULATIONS.md) using the brief's evidence template.

## Documentation

| File | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, data flow, data model, state machine, GL sync design |
| [API.md](API.md) | Endpoints, examples, errors, authentication, idempotency contract |
| [SIMULATIONS.md](SIMULATIONS.md) | Every simulation, with its evidence |
| [DECISIONS.md](DECISIONS.md) | Every design choice, assumption and known weakness, with a summary at the top |
| [fineract-customization/BUILD_NOTES.md](fineract-customization/BUILD_NOTES.md) | How the patched Fineract image is built |

## AI assistance

This project was built with Claude Code (Anthropic's Claude) as a coding assistant. It was used for:

- reading the brief and the pinned Fineract source and API, and proposing designs;
- drafting Python code, tests, simulation scripts and documentation;
- reviewing the implementation against the brief and finding gaps (for example the concurrent-repayment 409 in D-026 and the Fineract-id leak in D-027).

Project-specific instructions for the assistant are in `CLAUDE.md` and `.claude/skills/`. The Java patch was developed with the assistant's help; the author applied, built and committed it in the Fineract clone. Every change was reviewed by the author before it was committed, and the author can explain and modify any part of it.


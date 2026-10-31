SHELL := /bin/bash

FINERACT_IMAGE ?= fineract-custom:local
BUILD_LOG ?= evidence/build-fineract.log
DOCKER_BUILD_FLAGS ?=
# Fineract clone used to produce the patches; gitignored, never committed.
FINERACT_SRC ?= fineract-src
FINERACT_TAG := $(shell sed -n 's/^ARG FINERACT_TAG=//p' fineract-customization/Dockerfile)
PATCH_DIR := $(CURDIR)/fineract-customization/patch
JAVA_HOME_25 ?= $(shell /usr/libexec/java_home -v 25 2>/dev/null)

.PHONY: build-fineract patches up down reset logs ps bootstrap verify-fineract-setup evidence-bootstrap simulate-gl-sync simulate-erp simulate-loan-lifecycle simulate-concurrency simulate-failures simulate-reconciliation simulate-gl-reconciliation demo-fineract-gl evidence-compose-up test-java test-python lint

# Builds what changed and waits until every service reports healthy.
# Fineract is built through build-fineract (same builder and cache as before), not
# by compose, which may use a different builder and recompile Fineract from scratch.
# Fineract's first boot runs all Liquibase changesets, so allow up to 15 minutes.
up:
	@test -f .env || { echo "Missing .env: run 'cp .env.example .env' first"; exit 1; }
	$(MAKE) build-fineract
	docker compose build mock-erp integration-service
	docker compose up -d --no-build --wait --wait-timeout 900

down:
	docker compose down

# Deletes the postgres volume: the next `make up` starts from empty databases.
reset:
	docker compose down --volumes

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps

# Regenerates patch/*.patch from commits on top of FINERACT_TAG in the clone.
# Without a clone (e.g. a grader's machine) the committed patches are used as-is.
patches:
	@if [ -d "$(FINERACT_SRC)/.git" ]; then \
	  if [ -n "$$(git -C $(FINERACT_SRC) status --porcelain)" ]; then \
	    echo "WARNING: $(FINERACT_SRC) has uncommitted changes; they are NOT in the patches"; \
	  fi; \
	  rm -f $(PATCH_DIR)/*.patch; \
	  git -C $(FINERACT_SRC) format-patch --no-signature $(FINERACT_TAG) -o $(PATCH_DIR); \
	  echo "Patches: $$(ls $(PATCH_DIR)/*.patch 2>/dev/null | wc -l | tr -d ' ') from $(FINERACT_SRC) on $(FINERACT_TAG)"; \
	else \
	  echo "No Fineract clone at $(FINERACT_SRC): using committed patches"; \
	fi

# pipefail is set inside the recipe: macOS ships GNU Make 3.81, which ignores .SHELLFLAGS.
build-fineract: patches
	set -o pipefail; DOCKER_BUILDKIT=1 docker build --progress=plain --load $(DOCKER_BUILD_FLAGS) \
		-t $(FINERACT_IMAGE) fineract-customization 2>&1 | tee $(BUILD_LOG)

# Fineract setup through the REST API (get-or-create); safe to run any number of times.
# Credentials come from .env, so the API user's password matches what the integration service uses.
bootstrap:
	@test -f .env || { echo "Missing .env: run 'cp .env.example .env' first"; exit 1; }
	set -o pipefail; set -a; . ./.env; set +a; python3 scripts/bootstrap.py 2>&1 | tee evidence/bootstrap.log

verify-fineract-setup:
	set -o pipefail; set -a; . ./.env; set +a; python3 scripts/verify_fineract_setup.py 2>&1 | tee evidence/verify-fineract-setup.log

# Run on a fresh stack (make reset && make up): first run creates, second run changes nothing.
evidence-bootstrap:
	@test -f .env || { echo "Missing .env: run 'cp .env.example .env' first"; exit 1; }
	set -o pipefail; set -a; . ./.env; set +a; \
	  python3 scripts/bootstrap.py 2>&1 | tee evidence/bootstrap-run1.log; \
	  python3 scripts/bootstrap.py 2>&1 | tee evidence/bootstrap-run2.log; \
	  python3 scripts/verify_fineract_setup.py 2>&1 | tee evidence/verify-fineract-setup.log

simulate-gl-sync:
	set -o pipefail; python3 scripts/simulate_gl_sync.py 2>&1 | tee evidence/simulate-gl-sync.log

simulate-erp:
	set -o pipefail; python3 scripts/simulate_erp.py 2>&1 | tee evidence/simulate-erp.log

# Needs FINERACT_API_* from .env: Fineract's state after each stage is read directly from Fineract.
simulate-loan-lifecycle:
	set -o pipefail; set -a; . ./.env; set +a; python3 scripts/simulate_loan_lifecycle.py 2>&1 | tee evidence/simulate-loan-lifecycle.log

# Part I: duplicate and concurrent disbursements and repayments; counts rows in the ERP and integration databases.
simulate-concurrency:
	set -o pipefail; set -a; . ./.env; set +a; python3 scripts/simulate_concurrency.py 2>&1 | tee evidence/simulate-concurrency.log

# Part J: needs FAULT_INJECTION_ENABLED=true (in .env) on the integration service, and stops/starts mock-erp itself.
simulate-failures:
	set -o pipefail; set -a; . ./.env; set +a; python3 scripts/simulate_failures.py 2>&1 | tee evidence/simulate-failures.log

simulate-reconciliation:
	set -o pipefail; set -a; . ./.env; set +a; python3 scripts/simulate_reconciliation.py 2>&1 | tee evidence/simulate-reconciliation.log

simulate-gl-reconciliation:
	set -o pipefail; set -a; . ./.env; set +a; python3 scripts/simulate_gl_reconciliation.py 2>&1 | tee evidence/simulate-gl-reconciliation.log

demo-fineract-gl:
	set -o pipefail; python3 scripts/demo_fineract_gl_sync.py 2>&1 | tee evidence/demo-fineract-gl-sync.log

evidence-compose-up:
	scripts/capture_compose_up.sh 2>&1 | tee evidence/compose-up.log

# Runs the patch's JUnit tests in the Fineract clone (patches applied as commits).
# The integration test calls the running stack from `make up` on https://localhost:8443.
test-java:
	@test -d "$(FINERACT_SRC)/.git" || { echo "test-java needs the Fineract clone at $(FINERACT_SRC)"; exit 1; }
	set -o pipefail; cd $(FINERACT_SRC) && JAVA_HOME=$(JAVA_HOME_25) ./gradlew --console=plain \
		:fineract-accounting:test --tests '*GLAccountExternalReferenceMatcherTest' --tests '*GLAccountSyncTest' \
		:integration-tests:test --tests '*GLAccountExternalReferenceIntegrationTest' 2>&1 | tee $(CURDIR)/evidence/test-java.log

test-python:
	cd mock-erp && uv run pytest -q
	cd integration-service && uv run pytest -q

lint:
	cd mock-erp && uv run ruff check && uv run ruff format --check && uv run mypy
	cd integration-service && uv run ruff check && uv run ruff format --check && uv run mypy

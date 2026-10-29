SHELL := /bin/bash

FINERACT_IMAGE ?= fineract-custom:local
BUILD_LOG ?= evidence/build-fineract.log
DOCKER_BUILD_FLAGS ?=
# Fineract clone used to produce the patches; gitignored, never committed.
FINERACT_SRC ?= fineract-src
FINERACT_TAG := $(shell sed -n 's/^ARG FINERACT_TAG=//p' fineract-customization/Dockerfile)
PATCH_DIR := $(CURDIR)/fineract-customization/patch

.PHONY: build-fineract patches up down reset logs ps

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

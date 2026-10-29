SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

FINERACT_IMAGE ?= fineract-custom:local
BUILD_LOG ?= evidence/build-fineract.log

.PHONY: build-fineract

# Phase 3 adds patch regeneration from the Fineract clone before the build.
build-fineract:
	DOCKER_BUILDKIT=1 docker build --progress=plain \
		-t $(FINERACT_IMAGE) fineract-customization 2>&1 | tee $(BUILD_LOG)

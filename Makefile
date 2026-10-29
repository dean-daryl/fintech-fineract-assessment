SHELL := /bin/bash

FINERACT_IMAGE ?= fineract-custom:local
BUILD_LOG ?= evidence/build-fineract.log
DOCKER_BUILD_FLAGS ?=

.PHONY: build-fineract

# pipefail is set inside the recipe: macOS ships GNU Make 3.81, which ignores .SHELLFLAGS.
# Phase 3 adds patch regeneration from the Fineract clone before the build.
build-fineract:
	set -o pipefail; DOCKER_BUILDKIT=1 docker build --progress=plain --load $(DOCKER_BUILD_FLAGS) \
		-t $(FINERACT_IMAGE) fineract-customization 2>&1 | tee $(BUILD_LOG)

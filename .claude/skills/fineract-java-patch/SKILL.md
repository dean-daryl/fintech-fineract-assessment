---
name: fineract-java-patch
description: Use when changing Apache Fineract's Java code for the assessment: building from source at a pinned tag, writing Liquibase changesets, GL account logic, permissions, JUnit tests, and producing the patch file.
---

# Fineract Java patch workflow

The Java change must be small, targeted, and reproducible from a clean clone of the pinned tag. Reviewers grade the patch itself, so every edit should be one a Fineract maintainer would recognise.

## Before changing anything

1. Confirm the pinned tag from the repo README (`FINERACT_TAG` in `fineract-customization/Dockerfile`).
2. Work in the gitignored Fineract clone at `fineract-src/` in the assessment repo, on a branch made from the tag: `git checkout -b gl-external-ref <tag>`.
3. Read the existing code before designing. For GL accounts, start from the accounting module: the GL account entity, its JSON command handlers (create/update), the write service, the data validator, and the API resource. Note what the tag already stores (for example an existing external ID or tag field) and record it in DECISIONS.md.
4. Prefer extending the existing GL account command path over a standalone endpoint. It keeps Fineract's validation, permissions, transaction boundary and audit trail.

## Required behaviors (Part A)

- Persist the ERP `external_reference` on the GL account.
- Unique constraint on it at the database level.
- Same reference + identical payload: return the existing account, create nothing.
- Same reference + different payload: reject with 409 (map to Fineract's platform conflict error type).
- Concurrent identical creates: catch the unique-constraint violation, re-read by reference, then apply the two rules above. Never surface a 500.
- Protected by Fineract's permission model: register a permission code and check it.

## Schema changes

- Every schema change is a Liquibase changeset in the module's existing changelog structure. Follow the naming and numbering of neighbouring changesets.
- Never edit a changeset after it has run; add a new one. Liquibase checksums will fail otherwise.
- Never ship manual SQL.

## Tests

- At least one JUnit unit test (payload comparison and conflict decision as a pure method is easiest).
- At least one integration test against a real database, following the tag's existing integration-test style.
- Run them with one documented command, exposed as `make test-java`.

## Producing the patch

```bash
# inside the Fineract clone
git format-patch <tag> -o <assessment-repo>/fineract-customization/patch/
```

- Regenerate after every change; `make build-fineract` should do this before building.
- Exclude build output and IDE files.
- Before submitting, verify on a fresh clone: `git clone --branch <tag> ... && git apply --check patch/*.patch` (or `git am`).
- Save the build log and test output to `evidence/`.

## Interview readiness

For every hunk, be able to say why it lives there, which transaction it runs in, and what happens under concurrent requests. Practise one live change: add a field to the GL entity with a Liquibase changeset and a test.

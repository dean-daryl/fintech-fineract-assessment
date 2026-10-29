# Build Notes

## Building the Fineract image

```bash
make build-fineract            # log goes to evidence/build-fineract.log
```

The `Dockerfile` clones Fineract at `FINERACT_TAG`, fails if the tag does not point at `FINERACT_COMMIT`, and builds `:fineract-provider:bootJar` with the tag's own Gradle wrapper on Temurin JDK 25. The runtime image holds only the JRE and the jar.

- Only tests are skipped (`-x test -x cucumber`). Upstream's Docker CI also skips `buildJavaSdk`, but only because it reuses an already-built workspace; on a clean clone that task generates the Avro classes `fineract-core` compiles against, so it must run.
- Memory: the tag's `gradle.properties` requests `-Xmx12g`. The build replaces only that value with `GRADLE_XMX` (default `4g`) and keeps the other JVM flags, and limits Gradle to `GRADLE_MAX_WORKERS` (default `2`). Both are build args.
- Gradle dependencies are kept in a BuildKit cache mount, so rebuilds skip the download.

## Patches

Our Java change lives as `git format-patch` files in `patch/`. The Dockerfile applies every `patch/*.patch` in name order with `git apply --index` after the clone and before Gradle. A patch that does not apply cleanly fails the build at that step. With no patches, the log says `No patches in patch/: building stock Fineract <tag>`.

### Working on the patch

```bash
git clone --depth 1 --branch 1.16.1 https://github.com/apache/fineract.git ../fineract
cd ../fineract && git switch -c gl-external-ref
# edit, then commit; only committed changes become patches
cd - && make build-fineract
```

`make build-fineract` first runs `make patches`, which deletes `patch/*.patch` and regenerates them with `git format-patch <tag>` from the clone at `FINERACT_SRC` (default `../fineract`). It warns if the clone has uncommitted changes. If there is no clone (a grader's machine), the committed patches are used unchanged.

### Patch pipeline evidence

- `evidence/build-patch-apply.log`: a throwaway one-line patch is applied and the build succeeds.
- `evidence/build-patch-reject.log`: a patch that no longer matches the source fails the build at the apply step.

## Stock build evidence

The unpatched build log is `evidence/build-stock.log`, produced with:

```bash
make build-fineract BUILD_LOG=evidence/build-stock.log DOCKER_BUILD_FLAGS=--no-cache
```

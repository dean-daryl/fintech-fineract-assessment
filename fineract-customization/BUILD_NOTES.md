# Build Notes

## Building the Fineract image

```bash
make build-fineract            # log goes to evidence/build-fineract.log
```

The `Dockerfile` clones Fineract at `FINERACT_TAG`, fails if the tag does not point at `FINERACT_COMMIT`, and builds `:fineract-provider:bootJar` with the tag's own Gradle wrapper on Temurin JDK 25. The runtime image holds only the JRE and the jar.

- Gradle task exclusions (`-x test -x cucumber -x buildJavaSdk`) match upstream's Docker CI at this tag (`.github/workflows/build-docker.yml`).
- Memory: the tag's `gradle.properties` requests `-Xmx12g`. The build replaces only that value with `GRADLE_XMX` (default `4g`) and keeps the other JVM flags, and limits Gradle to `GRADLE_MAX_WORKERS` (default `2`). Both are build args.
- Gradle dependencies are kept in a BuildKit cache mount, so rebuilds skip the download.

## Stock build evidence

The unpatched build log is `evidence/build-stock.log`, produced with:

```bash
make build-fineract BUILD_LOG=evidence/build-stock.log
```

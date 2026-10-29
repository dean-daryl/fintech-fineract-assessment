# syntax=docker/dockerfile:1
# Fineract built from source at a pinned release tag, with patch/*.patch applied.
# Versions are documented in README.md ("Versions"); change them there too.

ARG FINERACT_TAG=1.16.1
ARG FINERACT_COMMIT=c643dab306fe29798951591ce825fd5e5272923e
ARG JDK_IMAGE=eclipse-temurin:25.0.4.1_1-jdk
ARG JRE_IMAGE=eclipse-temurin:25.0.4.1_1-jre

FROM ${JDK_IMAGE} AS builder
ARG FINERACT_TAG
ARG FINERACT_COMMIT
ARG GRADLE_XMX=4g
ARG GRADLE_MAX_WORKERS=2

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# Full git clone (not a tarball): the build's generateGitProperties task needs .git.
RUN git clone --depth 1 --branch "${FINERACT_TAG}" https://github.com/apache/fineract.git . \
 && actual="$(git rev-parse HEAD)" \
 && echo "Fineract ${FINERACT_TAG} at commit ${actual}" \
 && if [ "${actual}" != "${FINERACT_COMMIT}" ]; then \
      echo "ERROR: tag ${FINERACT_TAG} is ${actual}, expected ${FINERACT_COMMIT}" >&2; exit 1; \
    fi

# Phase 3: apply patch/*.patch here.

# Same task exclusions as upstream's Docker CI (build-docker.yml at this tag).
RUN --mount=type=cache,target=/root/.gradle \
    jvmargs="$(grep '^org.gradle.jvmargs=' gradle.properties | cut -d= -f2- | sed "s/-Xmx[0-9]*[gGmM]/-Xmx${GRADLE_XMX}/")" \
 && ./gradlew --no-daemon --console=plain --max-workers="${GRADLE_MAX_WORKERS}" \
      -Dorg.gradle.jvmargs="${jvmargs}" \
      :fineract-provider:bootJar -x test -x cucumber -x buildJavaSdk \
 && jar="$(ls fineract-provider/build/libs/fineract-provider-*.jar | grep -v -- '-plain.jar$')" \
 && cp "${jar}" /fineract-provider.jar \
 && echo "Built ${jar}"

FROM ${JRE_IMAGE}
ARG FINERACT_TAG
ARG FINERACT_COMMIT
LABEL org.opencontainers.image.title="fineract-custom" \
      org.opencontainers.image.version="${FINERACT_TAG}" \
      org.opencontainers.image.revision="${FINERACT_COMMIT}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /fineract-provider.jar /app/fineract-provider.jar

USER nobody
EXPOSE 8443
# JVM flags mirror the tag's Jib container config (fineract-provider/build.gradle).
ENTRYPOINT ["java", "-Duser.home=/tmp", "-Dfile.encoding=UTF-8", "-Duser.timezone=UTC", \
            "-Djava.security.egd=file:/dev/./urandom", "-jar", "/app/fineract-provider.jar"]

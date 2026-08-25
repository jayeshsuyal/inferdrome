# syntax=docker/dockerfile:1.7

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS builder

ARG UV_VERSION=0.8.17
WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# uv is only the locked-install tool; application dependencies come from the
# committed uv.lock and --frozen refuses to resolve or rewrite it.
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv sync --frozen --no-dev --no-editable

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS runner

ARG SOURCE_REPOSITORY_COMMIT=development-unpinned
ARG INFERDROME_VERSION=0.1.0.dev0
ARG BUILD_FLAVOR=development

# Proof/release images must carry an exact source identity. Development images
# may be built without one, but the label remains explicitly unpinned.
RUN case "${BUILD_FLAVOR}" in \
      development) ;; \
      proof|release) \
        printf '%s\n' "${SOURCE_REPOSITORY_COMMIT}" \
          | grep -Eq '^[0-9a-f]{40,64}$' \
          || { echo 'proof/release build requires a hexadecimal source commit' >&2; exit 1; }; \
        printf '%s\n' "${INFERDROME_VERSION}" \
          | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$' \
          || { echo 'proof/release build requires a version' >&2; exit 1; }; \
        ;; \
      *) echo 'unsupported build flavor' >&2; exit 1 ;; \
    esac

LABEL org.opencontainers.image.source="https://github.com/jayeshsuyal/inferdrome" \
      org.opencontainers.image.revision="${SOURCE_REPOSITORY_COMMIT}" \
      org.opencontainers.image.version="${INFERDROME_VERSION}" \
      org.opencontainers.image.title="Inferdrome runner" \
      org.opencontainers.image.description="Inferdrome measurement runner; serving runtime supplied separately" \
      com.inferdrome.image-purpose="runner-only"

ENV PATH="/opt/inferdrome-runtime/bin:/usr/local/bin:/usr/bin:/bin" \
    HOME="/home/inferdrome" \
    TMPDIR="/tmp/inferdrome" \
    PYTHONDONTWRITEBYTECODE="1" \
    PYTHONUNBUFFERED="1"

RUN useradd --uid 10001 --create-home --home-dir /home/inferdrome \
      --shell /usr/sbin/nologin inferdrome \
    && mkdir -p /evidence /tmp/inferdrome \
    && chown -R 10001:10001 /evidence /tmp/inferdrome /home/inferdrome

COPY --from=builder --chown=10001:10001 /build/.venv /opt/inferdrome-runtime

USER 10001:10001
WORKDIR /home/inferdrome
VOLUME ["/evidence"]
ENTRYPOINT ["/opt/inferdrome-runtime/bin/python", "-m", "inferdrome.runner"]

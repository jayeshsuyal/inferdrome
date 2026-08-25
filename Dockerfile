FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS builder

WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/inferdrome-runtime

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# The uv bootstrap is a fixed Linux/amd64 release archive with an exact
# checksum. Application dependencies still come only from the committed lock.
ADD --checksum=sha256:920cbcaad514cc185634f6f0dcd71df5e8f4ee4456d440a22e0f8c0f142a8203 \
    https://github.com/astral-sh/uv/releases/download/0.8.17/uv-x86_64-unknown-linux-gnu.tar.gz \
    /tmp/uv.tar.gz
RUN tar -xzf /tmp/uv.tar.gz --strip-components=1 -C /usr/local/bin \
    && rm /tmp/uv.tar.gz \
    && /usr/local/bin/uv sync --frozen --no-dev --no-editable

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

COPY --from=builder --chown=10001:10001 /opt/inferdrome-runtime /opt/inferdrome-runtime

# Proof/release labels must describe the package actually copied into the
# image, not merely a caller-supplied string.
RUN case "${BUILD_FLAVOR}" in \
      development) ;; \
      proof|release) \
        test "$(/opt/inferdrome-runtime/bin/python -c \
          'from inferdrome import __version__; print(__version__)')" \
          = "${INFERDROME_VERSION}" \
          || { echo 'packaged Inferdrome version does not match build identity' >&2; exit 1; } \
        ;; \
    esac \
    && test "$(/opt/inferdrome-runtime/bin/inferdrome --version)" = \
      "${INFERDROME_VERSION}" \
    || { echo 'final Inferdrome entrypoint does not match build identity' >&2; exit 1; }

USER 10001:10001
WORKDIR /home/inferdrome
VOLUME ["/evidence"]
ENTRYPOINT ["/opt/inferdrome-runtime/bin/inferdrome"]

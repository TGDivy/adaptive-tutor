FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv

FROM node:26.7.0-bookworm-slim@sha256:cd565714d4da3e84bfd341e31448f81d47c6362198f152345297c9c1154e6341 AS codex
ARG CODEX_VERSION=0.148.0
RUN npm install --global --omit=dev "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS github-cli
ARG GITHUB_CLI_VERSION=2.97.0
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && architecture="$(dpkg --print-architecture)" \
    && case "${architecture}" in \
        amd64) checksum="a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112" ;; \
        arm64) checksum="73ea440ecad9c9e284429997ee6f93577bc6f7bc6fba357ef62c53ad8fb641a5" ;; \
        *) echo "Unsupported GitHub CLI architecture: ${architecture}" >&2; exit 1 ;; \
    esac \
    && archive="gh_${GITHUB_CLI_VERSION}_linux_${architecture}.tar.gz" \
    && curl --fail --location --show-error --silent \
        "https://github.com/cli/cli/releases/download/v${GITHUB_CLI_VERSION}/${archive}" \
        --output "/tmp/${archive}" \
    && printf '%s  %s\n' "${checksum}" "/tmp/${archive}" | sha256sum --check --strict \
    && tar --extract --gzip --file "/tmp/${archive}" --directory /tmp \
    && install -m 0755 \
        "/tmp/gh_${GITHUB_CLI_VERSION}_linux_${architecture}/bin/gh" \
        /usr/local/bin/gh

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/adaptive-tutor
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY curricula ./curricula
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS runtime
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Adaptive Tutor" \
      org.opencontainers.image.description="Self-hosted Git-native adaptive learning engine" \
      org.opencontainers.image.source="https://github.com/TGDivy/adaptive-tutor" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/adaptive-tutor/bin:/usr/local/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/adaptive-tutor \
    CODEX_HOME=/var/lib/adaptive-tutor-codex \
    ADAPTIVE_TUTOR_CONFIG=/etc/adaptive-tutor/config.yaml

COPY --from=builder /opt/adaptive-tutor /opt/adaptive-tutor
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/bin/codex /usr/local/bin/codex
COPY --from=codex /usr/local/lib/node_modules/@openai/codex /usr/local/lib/node_modules/@openai/codex
COPY --from=github-cli /usr/local/bin/gh /usr/local/bin/gh

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 adaptive-tutor \
    && useradd --uid 10001 --gid adaptive-tutor --home-dir /var/lib/adaptive-tutor \
        --shell /usr/sbin/nologin adaptive-tutor \
    && install -d -m 0700 -o adaptive-tutor -g adaptive-tutor \
        /etc/adaptive-tutor /var/lib/adaptive-tutor /var/lib/adaptive-tutor-codex \
        /var/lib/adaptive-tutor-grader /var/lib/adaptive-tutor-grader/codex \
        /run/adaptive-tutor-grader

USER 10001:10001
WORKDIR /var/lib/adaptive-tutor
EXPOSE 8765
STOPSIGNAL SIGTERM
ENTRYPOINT ["adaptive-tutor"]
CMD ["--help"]

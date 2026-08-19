FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv

FROM node:22.18.0-bookworm-slim@sha256:752ea8a2f758c34002a0461bd9f1cee4f9a3c36d48494586f60ffce1fc708e0e AS codex
ARG CODEX_VERSION=0.148.0
RUN npm install --global --omit=dev "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

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

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates libstdc++6 \
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

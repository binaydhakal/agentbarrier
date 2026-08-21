# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.13-slim-bookworm

FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/agentbarrier
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    --extra mcp \
    --extra service \
    --extra slack \
    --extra postgres-binary \
    --extra observability

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.source="https://github.com/binaydhakal/agentbarrier" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PATH="/opt/agentbarrier/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 agentbarrier \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agentbarrier \
    && mkdir -p /var/lib/agentbarrier \
    && chown 10001:10001 /var/lib/agentbarrier
COPY --from=build --chown=10001:10001 /opt/agentbarrier /opt/agentbarrier
USER 10001:10001
WORKDIR /var/lib/agentbarrier
ENTRYPOINT ["agentbarrier"]
CMD ["--help"]

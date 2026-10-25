# syntax=docker/dockerfile:1
#
# Two targets:
#   slim    - compose, approve, send, source shopify (no Chromium)
#   browser - source olx + enrich (adds Chromium and its system libraries)
#
# Build the base once, then reuse it for both:
#   docker build --target slim -t forgesmith:latest .
#   docker build --target browser -t forgesmith-browser:latest .

# --------------------------------------------------------------------- build
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# README.md is required by pyproject.toml (hatchling readme).
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------------------------------------------------------------------- slim
FROM python:3.12-slim-bookworm AS slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Europe/Warsaw \
    PATH=/app/.venv/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src

RUN useradd -u 10001 -m forgesmith \
 && mkdir -p /app/data /app/data/outbox /app/data/backups \
 && chown -R 10001:10001 /app

# The code resolves PROJECT_ROOT from the installed package; the data path must
# therefore be explicit in every container (compose sets both variables).
ENV FORGESMITH_DATA_DIR=/app/data \
    FORGESMITH_DATABASE_URL=sqlite+aiosqlite:////app/data/forgesmith.db

USER 10001:10001
WORKDIR /app

# No ENTRYPOINT: every documented invocation passes the console script name
# explicitly (``docker run … forgesmith version``, ``compose run … app
# forgesmith send``). Compose's ``init: true`` supplies the PID 1 reaper.
CMD ["forgesmith", "version"]

# ------------------------------------------------------------------- browser
FROM slim AS browser

USER root

RUN /app/.venv/bin/playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*

USER 10001:10001

# Chromium cannot create its sandbox namespaces under Docker Desktop/WSL2.
# Scoped to this target so the host default remains sandboxed.
ENV FORGESMITH_BROWSER_NO_SANDBOX=true

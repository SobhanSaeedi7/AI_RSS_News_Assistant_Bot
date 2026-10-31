# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Four independent build pipelines, one per service.
#
# Each service has its own `<service>-builder` stage and its own final
# `<service>` stage. No stage is the parent of another service's stage: every
# stage starts directly from python:3.12-slim. That means
#
#   docker build --target bot .
#   docker build --target worker .
#   docker build --target agent .
#   docker build --target migrate .
#
# can each be built in isolation without producing or requiring another
# service's image. The duplication is intentional: it keeps the dependency
# sets and the build graphs visibly separate.
#
# Dependencies are selected with exactly one pyproject extra per service:
#
#   bot      shared + feedparser, httpx, python-telegram-bot, config
#   worker   shared + feedparser, python-telegram-bot, config
#   agent    shared + fastapi, uvicorn, config
#   migrate  shared + alembic
#
# "shared" is asyncpg + SQLAlchemy, imported by all four images.
#
# Each builder installs only its own extra into /opt/venv; the matching final
# stage copies that venv and nothing else. Build tools, uv and the source tree
# never reach a runtime image.
#
# The last stage is `bot`, so a plain `docker build .` still produces the
# historical bot image.
# ---------------------------------------------------------------------------

# ===========================================================================
# Migration job: Alembic + SQLAlchemy + asyncpg, plus the migration scripts.
# ===========================================================================
FROM python:3.12-slim AS migrate-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

# Pinned build-only resolver; matches the uv that generated uv.lock.
ARG UV_VERSION=0.12.22
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv venv /opt/venv --python python3.12 \
    && uv export --locked --no-dev --no-emit-project --extra migrate \
        --format requirements-txt --output-file requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache --no-deps --no-editable . \
    && rm -f requirements.txt

FROM python:3.12-slim AS migrate

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

COPY --from=migrate-builder /opt/venv /opt/venv
# Only the migration image needs the Alembic configuration and scripts.
COPY alembic.ini ./
COPY alembic/ ./alembic/

RUN useradd --create-home --uid 10001 appuser
USER appuser

ENTRYPOINT ["alembic"]
CMD ["upgrade", "head"]

# ===========================================================================
# Agent: FastAPI/Uvicorn + SQLAlchemy + asyncpg + settings.
# ===========================================================================
FROM python:3.12-slim AS agent-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

ARG UV_VERSION=0.12.22
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv venv /opt/venv --python python3.12 \
    && uv export --locked --no-dev --no-emit-project --extra agent \
        --format requirements-txt --output-file requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache --no-deps --no-editable . \
    && rm -f requirements.txt

FROM python:3.12-slim AS agent

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

COPY --from=agent-builder /opt/venv /opt/venv

RUN useradd --create-home --uid 10001 appuser
USER appuser

ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]
CMD ["agent"]

# ===========================================================================
# Worker: Telegram delivery + RSS parsing + SQLAlchemy + asyncpg + settings.
# ===========================================================================
FROM python:3.12-slim AS worker-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

ARG UV_VERSION=0.12.22
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv venv /opt/venv --python python3.12 \
    && uv export --locked --no-dev --no-emit-project --extra worker \
        --format requirements-txt --output-file requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache --no-deps --no-editable . \
    && rm -f requirements.txt

FROM python:3.12-slim AS worker

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

COPY --from=worker-builder /opt/venv /opt/venv

RUN useradd --create-home --uid 10001 appuser
USER appuser

ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]
CMD ["worker"]

# ===========================================================================
# Bot: Telegram + RSS parsing + HTTP client + SQLAlchemy + asyncpg + settings.
# Last stage on purpose: `docker build .` keeps producing the bot image.
# ===========================================================================
FROM python:3.12-slim AS bot-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

ARG UV_VERSION=0.12.22
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv venv /opt/venv --python python3.12 \
    && uv export --locked --no-dev --no-emit-project --extra bot \
        --format requirements-txt --output-file requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache --no-deps --no-editable . \
    && rm -f requirements.txt

FROM python:3.12-slim AS bot

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

COPY --from=bot-builder /opt/venv /opt/venv

RUN useradd --create-home --uid 10001 appuser
USER appuser

ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]
CMD ["bot"]

# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# One Dockerfile, one build target per service.
#
# The three services share most of their runtime (Python 3.12, SQLAlchemy,
# asyncpg, pydantic-settings), but each has exclusive dependencies:
#
#   bot      python-telegram-bot, feedparser, httpx
#   worker   python-telegram-bot, feedparser
#   agent    fastapi, uvicorn
#   migrate  alembic
#
# Keeping them in one file lets every target reuse the same `builder` stage
# (the uv install, the lockfile, and the source copy) and keeps the four images
# from drifting apart. `compose.yaml` selects a target per service; the last
# stage is `bot`, so a plain `docker build .` still produces the historical
# bot image.
#
# Nothing from the builder stages except /opt/venv reaches a runtime image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

# The resolver is a build-only tool: pinning it keeps the tool that reads
# uv.lock from changing underneath a build. The version matches the one that
# generated the lockfile and the uv_build backend required by pyproject.toml.
ARG UV_VERSION=0.12.22
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# Copy dependency metadata first so this layer caches on the lockfile alone.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# An empty virtual environment shared by every per-service dependency stage.
# Each child stage installs only its own extra on top of this layer.
RUN uv venv /opt/venv --python python3.12

# ---------------------------------------------------------------------------
# Dependency stages: one per image, each installing exactly one extra.
#   --locked      : fail instead of silently resolving a different set
#   --no-dev      : pytest/ruff/aiosqlite never reach a runtime image
#   --no-emit-project: the project is installed separately, as a wheel
#   --extra X     : only the dependencies that service imports
#   --no-deps     : the wheel install must not re-resolve the dependency set
#   --no-editable : the package lives in site-packages, so the runtime image
#                   does not need the source tree at all
# ---------------------------------------------------------------------------
FROM builder AS deps-bot
RUN uv export --locked --no-dev --no-emit-project --extra bot \
        --format requirements-txt --output-file requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache --no-deps --no-editable . \
    && rm -f requirements.txt

FROM builder AS deps-worker
RUN uv export --locked --no-dev --no-emit-project --extra worker \
        --format requirements-txt --output-file requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache --no-deps --no-editable . \
    && rm -f requirements.txt

FROM builder AS deps-agent
RUN uv export --locked --no-dev --no-emit-project --extra agent \
        --format requirements-txt --output-file requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache --no-deps --no-editable . \
    && rm -f requirements.txt

FROM builder AS deps-migrate
RUN uv export --locked --no-dev --no-emit-project --extra migrate \
        --format requirements-txt --output-file requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache --no-deps --no-editable . \
    && rm -f requirements.txt

# ---------------------------------------------------------------------------
# Runtime base: only the interpreter, a prepared virtual environment, and an
# unprivileged user. No compiler, no uv, no pip cache, no source tree.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Run the application as an unprivileged user. The app only writes to
# PostgreSQL, so it does not need ownership of any path inside the image.
RUN useradd --create-home --uid 10001 appuser

# --- one-off Alembic migration job -----------------------------------------
FROM runtime AS migrate
COPY --from=deps-migrate /opt/venv /opt/venv
# Only this image needs the Alembic configuration and migration scripts.
COPY alembic.ini ./
COPY alembic/ ./alembic/
USER appuser
ENTRYPOINT ["alembic"]
CMD ["upgrade", "head"]

# --- internal FastAPI answer service ---------------------------------------
FROM runtime AS agent
COPY --from=deps-agent /opt/venv /opt/venv
USER appuser
ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]
CMD ["agent"]

# --- RSS ingestion and delivery worker -------------------------------------
FROM runtime AS worker
COPY --from=deps-worker /opt/venv /opt/venv
USER appuser
ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]
CMD ["worker"]

# --- Telegram bot (also the default `docker build .` image) ----------------
FROM runtime AS bot
COPY --from=deps-bot /opt/venv /opt/venv
USER appuser
ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]
CMD ["bot"]

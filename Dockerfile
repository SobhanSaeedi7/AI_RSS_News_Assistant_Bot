# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: resolve the lockfile, build the project wheel, and install
# everything the application needs into a self-contained virtual environment.
# Nothing from this stage except /opt/venv reaches the final image.
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

# Install production dependencies and the project itself into /opt/venv.
#   --locked      : fail instead of silently resolving a different set
#   --no-dev      : pytest/ruff/aiosqlite never reach the runtime image
#   --no-emit-project: the project is installed separately, as a wheel
#   --no-editable : the package lives in site-packages, so the runtime image
#                   does not need the source tree at all
RUN uv venv /opt/venv --python python3.12 \
    && uv export --locked --no-dev --no-emit-project --format requirements-txt \
        --output-file requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache --no-editable . \
    && rm -f requirements.txt

# ---------------------------------------------------------------------------
# Runtime: only the interpreter, the prepared virtual environment, and the
# Alembic configuration. No compiler, no uv, no pip cache, no source tree.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Dependencies and the installed package.
COPY --from=builder /opt/venv /opt/venv

# Alembic configuration and migration scripts. The one-off `migrate` service
# runs the `alembic` console script, which is on PATH via /opt/venv/bin; no
# credentials are baked in, DATABASE_URL is supplied at runtime.
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Run the application as an unprivileged user. The app only writes to
# PostgreSQL, so it does not need ownership of any path inside the image.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# The same image runs the bot, the worker, or the agent depending on the
# Compose command: `bot` (default), `worker`, or `agent`.
ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]
CMD ["bot"]

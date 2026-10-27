# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install the dependency resolver used for the rest of this build. Runtime
# packages are locked through uv.lock; pinning uv as well keeps the tool that
# reads the lock from changing underneath a build. The default matches the uv
# version used to generate uv.lock and required by the uv_build backend in
# pyproject.toml.
ARG UV_VERSION=0.12.22
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# Copy dependency metadata first to preserve Docker layer caching.
COPY pyproject.toml uv.lock ./

# Export and install in one layer so the temporary requirements.txt never
# reaches the final image, and so this layer still caches on pyproject.toml and
# uv.lock alone.
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt \
    && uv pip install --system --no-cache -r requirements.txt \
    && rm -f requirements.txt

# Copy the README required by pyproject.toml and the application source.
COPY README.md ./
COPY src ./src

# Copy Alembic migration configuration and scripts so `alembic` can be run
# from inside the container without baking in secrets.
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Install the project itself, then remove the build-only resolver: nothing at
# runtime (entrypoint, alembic, or the application) uses uv.
RUN uv pip install --system --no-cache -e . \
    && pip uninstall --yes uv

# Run the application as an unprivileged user. The app only writes to
# PostgreSQL, so it does not need ownership of the source tree.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# The same image runs the bot, the worker, or the agent depending on the
# Compose command: `bot` (default), `worker`, or `agent`.
ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]
CMD ["bot"]

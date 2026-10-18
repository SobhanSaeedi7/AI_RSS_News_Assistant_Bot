# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# uv is used to install dependencies from the locked uv.lock file.
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv pip install --system --no-dev --frozen-lockfile

COPY src ./src
RUN uv pip install --system --no-dev --no-build-isolation -e .

# The bot and worker are separate Compose services started from this same
# image via command overrides (`main` for bot, `worker` for worker). No
# secrets are baked into the image; all configuration is read from the
# environment at runtime.
ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]
CMD ["main"]

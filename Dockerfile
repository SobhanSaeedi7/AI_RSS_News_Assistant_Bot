# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \ 
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install uv for reproducible dependency installation.
RUN pip install --no-cache-dir uv

# Copy dependency metadata first to preserve Docker layer caching.
COPY pyproject.toml uv.lock ./

RUN uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
RUN uv pip install --system --no-cache -r requirements.txt

# Copy the README required by pyproject.toml and the application source.
COPY README.md ./
COPY src ./src

# Install the project itself.
RUN uv pip install --system --no-cache -e .

# The same image runs the bot or worker depending on the Compose command.
ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]
CMD ["main"]
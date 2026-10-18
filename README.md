# AI RSS News Assistant Bot

A Telegram bot for RSS feed management, AI-powered news summarization,
automatic news delivery, and question answering grounded in stored news.

## Project status

Foundation only. The repository currently contains:

* A validated, environment-based configuration system (`src/ai_rss_news_assistant/config.py`).
* Application logging (`src/ai_rss_news_assistant/logging.py`).
* Minimal `bot`/`worker` entry points that wire up configuration and logging
  (`src/ai_rss_news_assistant/main.py`). RSS ingestion, Telegram handlers,
  database models/schema, LLM calls, summarization, and Q&A are **not**
  implemented yet and are tracked as future milestones.
* Docker infrastructure (`Dockerfile`, `compose.yaml`).
* Tests for the configuration system (`tests/`).

## Initial architecture

The application runs on Docker Compose with three services:

| Service | Responsibility |
|---------|----------------|
| `db` | PostgreSQL 16. Persistent shared application data (feeds, articles, users, delivery state). |
| `bot` | Telegram interactions and user-initiated news Q&A. Runs the `main` entry point. |
| `worker` | RSS feed polling, news processing/summarization, and scheduled delivery. Runs the `worker` entry point. |

The bot and worker are separate application processes but share the same
PostgreSQL database. Summarization and Q&A are separate Python modules/workflows
within the application, not separate containers or model-serving services.

```
+-----------+      +-----------+      +-----------+
|   bot     |      |  worker   |      |    db     |
| (Telegram)|      | (RSS/AI)  |      | (Postgres)|
+-----+-----+      +-----+-----+      +-----+-----+
      |                   |                   ^
      |                   |                   |
      +-------------------+-------------------+
                  shared persistent data
```

No Redis or additional infrastructure is included at this stage.

## Environment configuration

Copy `.env.example` to `.env` (gitignored) and fill in the values:

```sh
cp .env.example .env
# edit .env with your own values
```

Settings are read from environment variables at runtime and validated at
startup; required secrets such as `BOT_TOKEN` cause a clear early failure when
missing. Secrets are never hard-coded and never baked into the Docker image.

Key variables:

* `BOT_TOKEN` — Telegram bot token from @BotFather (required).
* `APP_ENV` — `development`, `staging`, or `production` (default `development`).
* `LOG_LEVEL` — `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (default `INFO`).
* `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` — database credentials.
* `DATABASE_URL` — PostgreSQL connection URL shared by bot and worker (required).

## Getting started with Docker Compose

1. Copy and configure the environment file:

   ```sh
   cp .env.example .env
   # Edit `.env` and set at least BOT_TOKEN and POSTGRES_PASSWORD.
   ```

2. Build and start the services:

   ```sh
   docker compose up --build
   ```

3. Stop the services:

   ```sh
   docker compose down
   ```

The `bot` and `worker` services wait for `db` to become healthy before
starting. PostgreSQL data persists in the named volume `db_data`.

## Database decision: PostgreSQL

PostgreSQL is selected as the persistent store because the bot and worker are
separate processes that share application data and may write concurrently.
PostgreSQL's multi-version concurrency control (MVCC) handles concurrent
readers and writers without blocking, and its client/server model fits two
independent processes sharing one database.

SQLite is simpler for embedded or single-process/local use, but it allows only
one writer at a time, which is a poor fit for a bot and worker writing
concurrently. See:

* When to use SQLite: https://www.sqlite.org/whentouse.html
* SQLite FAQ (concurrency limits): https://www.sqlite.org/faq.html
* PostgreSQL MVCC: https://www.postgresql.org/docs/current/mvcc.html

## Development

Tests and linting use the project's existing `uv` workflow (Python 3.12):

```sh
uv run pytest          # run the test suite
uv run ruff check      # lint
uv run ruff format     # format
```

## References

* Docker Compose: https://docs.docker.com/compose/

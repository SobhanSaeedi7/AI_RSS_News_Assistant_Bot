# AI RSS News Assistant Bot

A Telegram bot for RSS feed management, AI-powered news summarization,
automatic news delivery, and question answering grounded in the user's stored
news.

## Project status

Implemented:

* Validated environment configuration and application logging.
* Telegram commands: `/start`, `/help`, `/addfeed`, `/listfeeds`, `/delfeed`,
  `/news`, `/delivery`, `/ask`.
* Per-user RSS subscriptions and background ingestion by the `worker`.
* LLM summarization for automatic delivery, with an extractive fallback when
  no LLM key is configured.
* Per-user automatic delivery with an ON/OFF preference and delivery
  deduplication.
* A grounded `/ask` agent that answers only from the requesting user's stored
  news and cites source titles and URLs.
* Alembic migrations, Docker Compose, and a pytest/ruff test suite.

Not implemented: conversation history for `/ask` (it is stateless), semantic
search/embeddings, and coordination between multiple worker replicas.

## Architecture

The application runs on Docker Compose with three long-running services plus a
one-off migration service:

| Service | Responsibility |
|---------|----------------|
| `db` | PostgreSQL 16. Persistent shared application data (feeds, articles, users, delivery state). |
| `migrate` | One-off Alembic `upgrade head` job. It exits after applying schema changes; `bot` and `worker` wait for it to complete successfully. |
| `bot` | Telegram interactions, `/ask` grounded answering, and delivery preference commands. Runs the `main` entry point. |
| `worker` | RSS feed polling, news processing/summarization, and scheduled automatic delivery. Runs the `worker` entry point. |

The bot and worker are separate application processes but share the same
PostgreSQL database. Summarization and Q&A are Python modules within the
application, not separate containers or model-serving services. The `migrate`
service is not a long-running application service.

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

No Redis or additional infrastructure is included.

## Telegram commands

| Command | Description |
|---------|-------------|
| `/start` | Register and show the welcome message. |
| `/help` | Show available commands. |
| `/addfeed <url>` | Subscribe to an RSS feed. |
| `/listfeeds` | List your feeds and their status. |
| `/delfeed <id>` | Delete one of your feeds. |
| `/news` | Show your latest stored articles. |
| `/delivery` | Show whether automatic delivery is on. |
| `/delivery on` / `/delivery off` | Enable or disable automatic delivery for your account. |
| `/ask <question>` | Ask a question about your stored news. |

All commands operate on the calling Telegram user's own data only.

## Asking questions with `/ask`

```text
/ask <question>
```

Example: `/ask مهم‌ترین خبرهای فناوری این هفته چه بود؟`

How an answer is produced:

1. The question is normalized and split into search terms; common Persian and
   English stopwords are dropped (`agent.extract_terms`).
2. `db.repository.search_news_items` runs a case-insensitive keyword query
   over the **requesting user's** news titles, summaries, stored content, and
   feed titles. Ownership is enforced in the SQL query itself, not in the
   prompt or the Telegram handler.
3. Candidates are re-ranked by the number of distinct query terms they
   contain, then by recency, and at most five are kept.
4. If no candidate matches, the bot replies that there is not enough
   information and **does not call the LLM**.
5. Otherwise the numbered sources (title, feed, publication date, URL, and a
   bounded excerpt) are sent to the configured OpenAI-compatible provider.
   The reply is returned in Persian with a clickable source list.

The agent has no web access, no tools, and no ability to read another user's
data. RSS text is treated as untrusted: newlines are collapsed, lookalike
`<sources>` delimiters are neutralized, and the text is placed inside a real
`<sources>` block whose system prompt says to ignore instructions, fake
delimiters, and fake source metadata inside it. Source URLs are accepted only
when they are valid `http(s)` URLs without whitespace, control characters, or
embedded credentials, and all stored text is HTML-escaped before rendering.
Prompt injection cannot be eliminated by prompting alone, but the model
receives no tools and no other user's data, so the impact is limited to the
wording of one answer.

### Retrieval limitations

Retrieval is deterministic keyword matching, not semantic search. Synonyms,
paraphrases, and morphological variants (for example plural forms) may be
missed. PostgreSQL's built-in full-text search is not used because the stock
image has no Persian text-search configuration. The candidate set is bounded,
so questions spanning many topics may miss older articles. This trade-off
avoids a vector database, embeddings, and an additional infrastructure
service.

## Automatic delivery

The worker polls feeds, stores new items, and — after the ingestion
transaction commits — delivers undelivered items to each user whose
`auto_delivery_enabled` preference is on. `/delivery` shows the status and
`/delivery on|off` changes it; the preference is stored in PostgreSQL and
survives restarts.

Delivery records live in `news_deliveries` with a unique
`(user_id, news_item_id)` constraint, so an item is not delivered twice under
normal operation. Semantics are **at-least-once**: if Telegram accepts a
message but the delivery record fails to commit, the item is retried on the
next poll and the user may see it twice.

Delivery OFF skips both the LLM request and the Telegram send for that user.
A failed, blocked, or rejected send is isolated per item and per user and
cannot stop other users' deliveries. Transient Telegram and summarization
errors are retried on a later poll; a permanent `BadRequest` rejection is
recorded so the same item does not block the queue; a blocked chat pauses
delivery for that user for the current run.

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

* `BOT_TOKEN` — Telegram bot token from @BotFather (required by `bot`; also
  used by `worker` to send automatic delivery messages).
* `APP_ENV` — `development`, `staging`, or `production` (default `development`).
* `LOG_LEVEL` — `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (default `INFO`).
* `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` — database credentials.
* `DATABASE_URL` — PostgreSQL connection URL shared by bot and worker.
* `LLM_API_KEY` — optional key for an OpenAI-compatible provider.
* `LLM_BASE_URL` — provider base URL (default `https://api.avalai.ir/v1`).
* `LLM_MODEL` — chat model name (default `gpt-6-luna`).
* `LLM_TIMEOUT_SECONDS` — timeout for one provider request (default `30`).
* `POLL_INTERVAL_SECONDS` — seconds between worker RSS polls (default `300`).
* `FETCH_FULL_TEXT` — fetch full article text when `true` (default `true`).
* `FETCH_TIMEOUT_SECONDS` — article-fetch timeout in seconds (default `30`).

The `bot` service needs the LLM variables for `/ask`. The `worker` service
needs them for LLM summaries; without a key it delivers trimmed article text
instead. When the key is missing and matching news exists, `/ask` replies that
AI answers are disabled; with no matching news it reports insufficient
evidence instead. An empty `BOT_TOKEN` is treated as unset, so the worker can
still ingest news with automatic delivery disabled.

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

   Compose starts `db`, waits for it to become healthy, runs the one-off
   `migrate` service, and starts `bot` and `worker` only after migrations
   succeed. Use `-d` to run in the background.

3. Check status and follow logs:

   ```sh
   docker compose ps
   docker compose logs -f
   docker compose logs -f bot worker
   ```

4. Restart or recreate after changes:

   ```sh
   docker compose restart bot worker   # restart without recreating
   docker compose up --build -d        # rebuild/recreate after code or env changes
   ```

   `restart` does not apply new migrations; use `docker compose up --build -d`
   after schema changes.

5. Stop the services:

   ```sh
   docker compose down
   ```

`docker compose down` keeps the named PostgreSQL volume. Do not add `-v`
unless you intentionally want to delete stored data. PostgreSQL data persists
in the named volume `db_data`.

## Database migrations

Schema changes are managed with [Alembic](https://alembic.sqlalchemy.org/). The
migration scripts live in `alembic/versions/`. The one-off `migrate` service
runs `alembic upgrade head` against the `db` service and exits. `bot` and
`worker` declare a `service_completed_successfully` dependency on `migrate`, so
`docker compose up` applies pending migrations before the application services
start. The `migrate` service is the only component that applies schema changes;
use `docker compose run --rm migrate` to run it explicitly without starting the
long-running services.

### Prerequisites

* Docker and Docker Compose installed.
* A `.env` file (copied from `.env.example`) with `POSTGRES_PASSWORD` and
  `BOT_TOKEN` set. `.env` is gitignored and excluded from the Docker build
  context by `.dockerignore`.

### Apply migrations

```sh
docker compose up migrate
```

This builds the image (if needed), waits for the `db` service to become
healthy, then runs `alembic upgrade head` against it and exits. `docker compose
up --build` also runs this service automatically before starting `bot` and
`worker`. The `migrate` service uses `DATABASE_URL` composed from
`POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`; no credentials are
hard-coded or logged.

Equivalent one-off form:

```sh
docker compose run --rm migrate
```

### Inspect the current revision

```sh
docker compose run --rm migrate current
```

This prints the latest applied migration revision (e.g. `0002_add_normalized_url`).

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

## Design decisions and rationale

* **Ownership in SQL, not in prompts.** Every repository query that touches
  user-owned rows filters by the internal `user_id`. Telegram handlers only
  resolve the caller's Telegram ID to that internal ID; question text and model
  output are never used as authorization.
* **Keyword retrieval instead of embeddings.** It works for Persian and
  English without new services, is deterministic, and is easy to test. The
  cost is weaker recall for paraphrased questions, which is documented above.
* **The LLM is optional.** Delivery degrades to extractive summaries and
  `/ask` reports that AI answers are unavailable, so a missing key never
  produces invented answers.
* **Bounded prompts and outputs.** At most five sources, bounded excerpts, and
  a bounded answer length keep requests predictable and fit Telegram limits.
* **Untrusted RSS content.** Retrieved text is delimited and explicitly marked
  as data in the system prompt. The agent has no tools, so injected text
  cannot trigger database access, web requests, or cross-user reads.
* **At-least-once delivery.** The unique delivery record prevents normal
  duplicates, but a crash between the Telegram send and the database commit can
  resend an item. Exactly-once delivery is not claimed.
* **Graceful Telegram failures.** Blocked chats, rejected messages, and API
  errors are logged and isolated so one user cannot stop the worker.

## Development

Tests and linting use the project's existing `uv` workflow (Python 3.12):

```sh
uv run pytest -q -p no:warnings      # run the test suite
uv run ruff check .                  # lint
uv run ruff format --check <files>   # formatting check on changed files
```

Tests use SQLite (in-memory or temporary files) and fake Telegram/LLM
collaborators; they do not send real messages or consume API credits. The
test suite does not require PostgreSQL, Docker, a bot token, or an LLM key.

## Troubleshooting

* **`bot` exits with “BOT_TOKEN is required”** — set a non-empty `BOT_TOKEN` in
  `.env`; an empty value is treated as unset.
* **`migrate` fails or `bot`/`worker` never start** — inspect
  `docker compose logs migrate` and confirm `POSTGRES_PASSWORD` is set. The
  application services deliberately wait for a successful migration.
* **`/ask` says AI answers are disabled** — set `LLM_API_KEY`, or check the
  provider URL/model. `/news` and automatic delivery still work without a key.
* **No automatic messages arrive** — confirm `/delivery` is on, inspect
  `docker compose logs -f worker`, and check `POLL_INTERVAL_SECONDS`. A user
  who blocked the bot will not receive messages.
* **Database data appears missing** — check that the named volume `db_data`
  still exists; `docker compose down -v` deletes it intentionally.

## Known limitations

* Delivery is at-least-once, not exactly-once (see above).
* Retrieval is keyword-based, not semantic; synonyms and paraphrases can be
  missed.
* Prompt-injection defenses are best-effort; the agent has no tools and no
  access to other users' data, but model wording can still be influenced.
* One worker process is assumed; multiple worker replicas are not coordinated.
* RSS/article fetching is best-effort and bounded by time and response-size
  limits; some publishers may block the crawler or return partial text.

## References

* Docker Compose: https://docs.docker.com/compose/
* Alembic: https://alembic.sqlalchemy.org/

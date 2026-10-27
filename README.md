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
* A dedicated `agent` service that answers only from the requesting user's
  stored news and cites source titles and URLs, reached by the bot over internal
  HTTP with a shared bearer token.
* Alembic migrations, Docker Compose, and a pytest/ruff test suite.

Not implemented: conversation history for `/ask` (it is stateless), semantic
search/embeddings, and coordination between multiple worker replicas.

## Architecture

The application runs on Docker Compose with three long-running services plus a
one-off migration service:

| Service | Responsibility |
|---------|----------------|
| `db` | PostgreSQL 16. Persistent shared application data (feeds, articles, users, delivery state). |
| `migrate` | One-off Alembic `upgrade head` job. It exits after applying schema changes; every application service waits for it to complete successfully. |
| `bot` | Telegram interactions: commands, feed management, `/news`, the delivery preference. It answers `/ask` by calling the `agent` service over internal HTTP and never calls a provider itself. Runs the `bot` entry point. |
| `worker` | RSS feed polling, news processing/summarization, and scheduled automatic delivery. Runs the `worker` entry point. |
| `agent` | The internal answer API: grounded retrieval over one user's stored news, the provider call, and source metadata. It has no Telegram, RSS, or delivery code. Runs the `agent` entry point. |

The three application services run the same image, share one PostgreSQL
database, and share nothing else. The `agent` publishes **no host port**: it is
reachable only on the Compose network, and every request must present the shared
`AGENT_INTERNAL_TOKEN` bearer token.

```
+-------------+   POST /answer + bearer token   +-------------+
|    bot      | -------------------------------> |    agent    |
| (Telegram)  | <------------------------------- | (retrieval  |
+------+------+        answer JSON (200/4xx/5xx) |  + provider)|
       |                                          +------+------+
       |             +-------------+                     |
       +-----------> |     db      | <-------------------+
                     | (PostgreSQL)|
                     +-------------+
                            ^
                            |
                     +------+------+
                     |   worker    |
                     | (RSS, AI    |
                     |  summaries, |
                     |  delivery)  |
                     +-------------+
```

### Why HTTP between the bot and the agent

* The boundary is real, not just a convention. The bot cannot import retrieval or
  provider code, so a Telegram process can never call a model; the agent cannot
  import Telegram, so it can never send a message. Architecture tests enforce
  both directions.
* `/ask` degrades gracefully. If the agent is down, slow, or not configured, the
  bot answers with a clear Persian message while `/news`, feed management, and
  automatic delivery keep working.
* Each concern has its own lifecycle. The agent can be restarted, or pointed at a
  different model, without touching Telegram polling, and no second image is
  needed.

### Why no broker and no vector database

One question produces one bounded answer, so a direct HTTP call with a
40-second outer timeout (`AGENT_TIMEOUT_SECONDS`) is sufficient: there is no
fan-out, nothing to queue, and no second consumer of answers. A broker would add
a service to operate without removing a real limitation. Retrieval is
deterministic keyword matching over the PostgreSQL database the services already
share, so no vector store, embedding service, or search cluster is required
(see *Retrieval limitations* for the trade-off this accepts).

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

1. The bot validates the question (non-empty and at most 500 characters) and
   sends it to the `agent` service over internal HTTP together with the caller's
   internal user id and the shared bearer token.
2. The agent normalizes the question and splits it into search terms; common
   Persian and English stopwords are dropped (`agent.extract_terms`).
3. `db.repository.search_news_items` runs a case-insensitive keyword query
   over the **requesting user's** news titles, summaries, stored content, and
   feed titles. Ownership is enforced in the SQL query itself, not in the
   prompt or the Telegram handler.
4. Candidates are re-ranked by the number of distinct query terms they
   contain, then by recency, and at most five are kept.
5. If no candidate matches, the agent returns the `insufficient_evidence`
   answer and **does not call the LLM**.
6. Otherwise the numbered sources (title, feed, publication date, URL, and a
   bounded excerpt) are sent to the configured OpenAI-compatible provider.
   The reply travels back to the bot with a clickable source list.

How failures surface:

| Agent outcome | HTTP status | What the user sees |
|---------------|-------------|--------------------|
| grounded answer | `200` | the answer plus sources |
| not enough evidence | `200` | the "not enough information" message, no sources |
| empty question | `200` | the "please write your question" message |
| provider failed | `502` | the generic "try again later" message |
| provider timed out | `504` | the generic "try again later" message |
| answering unavailable / bad token | `503` / `401` | the "AI answers are not available" message |
| invalid request | `422` | the generic "try again later" message (a bug on our side) |
| unexpected failure | `500` | the generic "try again later" message |

The bot renders only these canonical messages. It never forwards a response
body, an exception, the bearer token, or a provider message to Telegram, and it
never retries automatically: the user can simply ask again.

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
Each service validates only the settings it uses, so the worker starts without
`BOT_TOKEN` (delivery is disabled with a warning) and the bot starts without an
agent token (`/ask` reports that answers are unavailable).

Key variables:

* `BOT_TOKEN` — Telegram bot token from @BotFather (required by `bot`; also
  used by `worker` to send automatic delivery messages).
* `AGENT_INTERNAL_TOKEN` — shared bearer token for the internal agent API. The
  `bot` sends it and the `agent` requires it; use the same long random value for
  both (for example `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
  While it is empty the agent refuses every request (503) rather than serving
  unauthenticated traffic, and the bot answers `/ask` with the "unavailable"
  message.
* `AGENT_BASE_URL` — where the bot reaches the agent (default
  `http://agent:8000`).
* `AGENT_HOST`, `AGENT_PORT` — bind address and port of the agent inside the
  container network (defaults `0.0.0.0` and `8000`). The agent publishes no host
  port; do not expose it publicly.
* `AGENT_TIMEOUT_SECONDS` — outer timeout for one bot → agent request (default
  `40`). Keep it larger than `LLM_TIMEOUT_SECONDS` so the agent can report its
  own provider timeout first.
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

The `agent` service needs the LLM variables to answer `/ask`; the `bot` does not
read them at all. The `worker` needs them for LLM summaries; without a key it
delivers trimmed article text instead. When the key is missing and matching news
exists, `/ask` replies that AI answers are unavailable; with no matching news it
reports insufficient evidence instead. An empty `BOT_TOKEN` is treated as unset,
so the worker can still ingest news with automatic delivery disabled.

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
   `migrate` service, and starts `bot`, `worker`, and `agent` only after
   migrations succeed. The bot additionally waits for the agent's healthcheck.
   Use `-d` to run in the background.

   `bot`, `worker`, `agent`, and `migrate` all run the same image, defined once
   in `compose.yaml` and tagged `ai-rss-news-assistant:local`, so the services
   cannot drift apart.

3. Check status and follow logs:

   ```sh
   docker compose ps
   docker compose logs -f
   docker compose logs -f bot worker agent
   ```

   `docker compose ps` shows `agent` as `healthy` once its `/health` endpoint
   answers. `bot` and `worker` declare no healthcheck (they expose no port);
   their container state and logs are the signal to use.

4. Restart or recreate after changes:

   ```sh
   docker compose restart bot worker agent   # restart without recreating
   docker compose up --build -d              # rebuild/recreate after code or env changes
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

### Container image

* Dependencies are installed from `uv.lock` with `uv export --locked --no-dev`.
  Development dependencies, the exporter tool, and its temporary requirement
  list are not part of the final image.
* The container runs as the unprivileged `appuser` (uid 10001). The application
  reads its own files and writes only to PostgreSQL, so it needs no writable
  path inside the image.
* Logs are written to stdout unbuffered, so `docker compose logs` and the
  default Docker logging driver capture them without extra configuration.

## Database migrations

Schema changes are managed with [Alembic](https://alembic.sqlalchemy.org/). The
migration scripts live in `alembic/versions/`. The one-off `migrate` service
runs `alembic upgrade head` against the `db` service and exits. `bot`, `worker`,
and `agent` declare a `service_completed_successfully` dependency on `migrate`,
so `docker compose up` applies pending migrations before the application
services start. The `migrate` service is the only component that applies schema changes;
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

* **Internal HTTP for the answer service.** The bot and agent are separate
  processes that must not share a call stack. HTTP with a shared bearer token is
  the smallest boundary that enforces this, keeps the provider out of the
  Telegram process, and lets `/ask` degrade instead of failing when the agent is
  unwell.
* **The agent is not exposed.** It publishes no host port, and an unset
  `AGENT_INTERNAL_TOKEN` means "refuse everything" rather than "no authentication
  required". Ids in requests are internal user ids, so a caller cannot name a user
  it does not already own.
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
* **Statuses, not message parsing.** Provider failures are classified as
  `timeout` or `provider` at the client, carried through the summarization
  wrapper, mapped to HTTP statuses by the agent, and rendered from the shared
  contract, so no layer ever has to read exception text to decide what to say.
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
* **`docker compose ps` shows `db` as `healthy` but `bot`/`worker` as plain
  `running`** — that is expected. The application services expose no endpoint to
  probe, so they deliberately declare no healthcheck; the restart policy, the
  container state, and `docker compose logs -f bot worker` are the signals to
  use.
* **A `bot` container that keeps restarting** — a fatal configuration problem
  (for example a rejected `BOT_TOKEN`) logs one error line and exits, and
  `restart: unless-stopped` starts it again, so the same line repeats. The
  repeating fatal line in `docker compose logs bot` is the cause to fix.
* **`bot` restarts with `telegram.error.NetworkError` or `httpx.ConnectError`**
  — the container cannot reach `api.telegram.org`; check outbound HTTPS from the
  Docker host (proxy, VPN, or network filtering). The bot exits when its startup
  call to Telegram fails, and `restart: unless-stopped` retries it, so the
  service comes back on its own once connectivity returns. The worker tolerates
  the same failure: it logs a warning, keeps ingesting, and only skips automatic
  delivery.
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
* One worker process and one agent process are assumed; neither is coordinated
  across replicas.
* A bot → agent request is not queued or retried: a timeout or an unreachable
  agent becomes a user-visible "try again". There is no broker, so nothing
  survives a bot restart.
* Rotating `AGENT_INTERNAL_TOKEN` requires restarting both the `agent` and the
  `bot` services; requests made in between are rejected with 401.
* RSS/article fetching is best-effort and bounded by time and response-size
  limits; some publishers may block the crawler or return partial text.

## References

* Docker Compose: https://docs.docker.com/compose/
* Alembic: https://alembic.sqlalchemy.org/
* FastAPI: https://fastapi.tiangolo.com/
* Uvicorn: https://www.uvicorn.org/

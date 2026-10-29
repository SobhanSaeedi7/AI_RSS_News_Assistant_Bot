# 📰 AI RSS News Assistant Bot

**Your personal AI-powered news assistant on Telegram.**

Manage RSS feeds, receive scheduled news summaries, and ask questions about your collected news — all in one place.

<p align="center">

🤖 Telegram Bot · 🧠 AI-Powered · 🐳 Docker Compose · 🔒 User Isolation

</p>

## ✨ Features

- **📡 RSS Management** — Add, list, and remove RSS/Atom feeds with URL validation.
- **📝 AI News Summaries** — Receive concise, automatically generated summaries of new articles.
- **⏰ Personalized Delivery** — Toggle automatic delivery and configure your own schedule.
- **💬 Ask Your News** — Ask questions in natural language and get answers grounded in your stored articles.
- **🔗 Relevant Sources** — Answers include only the sources actually cited.
- **♻️ Duplicate Prevention** — Prevent repeated articles from being stored or normally delivered to the same user.
- **🛡️ Per-User Isolation** — Each user's feeds, articles, and deliveries remain scoped to their account.
- **🔄 Resilient Processing** — Feed retry backoff, Telegram error handling, and coordination between question answering and automatic delivery.
- **🐳 One-Command Deployment** — Docker Compose builds and runs the services and applies database migrations automatically.

## 🚀 Quick Start

### 1. Prerequisites

Install [Docker](https://docs.docker.com/get-docker/) with Docker Compose support and create a Telegram bot through [@BotFather](https://t.me/BotFather).

### 2. Configure the environment

Copy the example environment file:

```bash
cp .env.example .env
```

On Windows PowerShell, use:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and configure at least:

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Telegram bot token from BotFather |
| `POSTGRES_PASSWORD` | PostgreSQL password |
| `AGENT_INTERNAL_TOKEN` | Shared secret for internal bot-to-agent authentication |
| `LLM_API_KEY` | Optional API key for AI-generated summaries and answers |

Use the same strong, random `AGENT_INTERNAL_TOKEN` for the bot and agent. See `.env.example` for the available settings and defaults. Never commit `.env` or share your secrets.

### 3. Run the entire project

```bash
docker compose up --build -d
```

**That's it!** Docker Compose builds the application image, starts PostgreSQL, applies pending database migrations, and starts the Bot, Worker, and Agent services.


## 🤖 Telegram Commands

| Command | Description |
|---|---|
| `/start` | Register and display the welcome message |
| `/help` | Show available commands and usage |
| `/addfeed [url]` | Add an RSS/Atom feed |
| `/listfeeds` | List your feeds and their health status |
| `/delfeed [id\|url]` | Remove one of your feeds |
| `/news` | Browse your latest stored articles |
| `/delivery` | Toggle automatic news delivery ON/OFF |
| `/schedule` | Show your delivery schedule and usage |
| `/schedule <minutes>` | Set your delivery interval in minutes |
| `/ask <question>` | Ask a question about your stored news |
| `/cancel` | Cancel a pending feed operation |

**Feed management:** `/addfeed` and `/delfeed` accept an inline argument or prompt for it in a second step. Pending prompts expire after five minutes and can be cancelled with `/cancel`.

**Schedule interval:** `/schedule` takes exactly one non-negative integer number of minutes, for example `/schedule 30` or `/schedule 60`. The accepted range is `0` to `35,791,394` minutes (the largest whole number of minutes that fits in the database's 32-bit seconds column). The default `0` means no extra throttling: new articles are eligible on every worker poll. A positive value is the minimum spacing between automatic deliveries for that user and does not change the global RSS polling interval.

Delivery is evaluated during the worker's polling loop (default every 5 minutes), not by a separate per-user timer. Actual delivery therefore happens on the first poll at or after the requested interval; an interval below 5 minutes cannot guarantee delivery every minute. `/schedule` without an argument shows the current interval and usage.

## 🏗️ Architecture

The application uses separate services with clearly defined responsibilities:

| Service | Responsibility |
|---|---|
| **Bot** | Telegram commands and user interactions |
| **Worker** | RSS polling, article processing, summaries, and scheduled delivery |
| **Agent** | Retrieval and AI-powered question answering |
| **PostgreSQL** | Persistent application data |
| **Migration Job** | Applies database schema migrations before application startup |

The Bot communicates with the Agent over authenticated internal HTTP. All application services share the database, while the Agent is not exposed through a public host port.

### Technology Stack

- **Python 3.12**
- **Telegram Bot API**
- **PostgreSQL**
- **SQLAlchemy & Alembic**
- **FastAPI & HTTPX**
- **OpenAI-compatible LLM API**
- **Docker & Docker Compose**
- **pytest & Ruff**

## 🔐 Security & Reliability

- Database-level ownership checks help prevent access to other users' data.
- RSS URLs are validated, and private or reserved network destinations are rejected.
- Internal Agent requests require bearer-token authentication.
- RSS content is treated as untrusted input; the Agent has no web-search tools.
- Blocked Telegram recipients are paused until they contact the bot again.
- Failed sends are isolated so one recipient does not stop other users' deliveries.
- Automatic delivery preferences and schedules persist across restarts.


## ⚙️ Design Decisions

- **PostgreSQL:** Shared persistent storage for independent services and concurrent database access.
- **Internal HTTP:** Keeps Telegram handling separate from retrieval and provider calls.
- **Keyword-based retrieval:** Avoids the additional cost and infrastructure of embeddings and a vector database.
- **Polling:** Collects RSS updates without requiring a publicly accessible webhook endpoint.
- **Optional LLM:** Without an API key, automatic delivery falls back to trimmed article text; AI-powered `/ask` answers are unavailable.

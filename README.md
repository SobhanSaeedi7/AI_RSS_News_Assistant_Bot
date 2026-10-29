# 📰 AI RSS News Assistant Bot

**Your personal AI-powered news assistant on Telegram.**

Manage RSS feeds, receive news summaries, control delivery schedules, and ask questions about your stored news — all in one place.

## ✨ Features

- 📡 Add, list, and delete RSS/Atom feeds
- 🤖 AI-generated summaries with an extractive fallback
- 🔔 Personalized automatic news delivery
- ⏱️ Per-user delivery schedules
- 💬 Ask questions about stored news
- 🔗 Answers with verified article sources
- 🛡️ Per-user data isolation and RSS URL validation
- 🔄 Duplicate prevention and failure handling
- 🐳 Docker Compose deployment

## 🏗️ Architecture

| Service | Responsibility |
|---|---|
| **Bot** | Handles Telegram commands and user requests |
| **Worker** | Fetches RSS feeds, stores articles, summarizes, and delivers news |
| **Agent** | Retrieves a user's stored news and answers questions |
| **PostgreSQL** | Stores users, feeds, articles, preferences, and conversations |
| **Migration Job** | Applies database migrations at startup |

Services communicate through internal HTTP and PostgreSQL. No message broker or vector database is required.

## 🚀 Quick Start

### Requirements

- Docker Engine and Docker Compose
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An LLM API key for AI-generated answers and summaries

### 1. Configure environment variables

**Windows PowerShell:**

```powershell
Copy-Item .env.example .env
```

**Linux/macOS:**

```bash
cp .env.example .env
```

Configure these values in `.env`:

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Telegram bot token |
| `POSTGRES_PASSWORD` | Database password |
| `AGENT_INTERNAL_TOKEN` | Shared Bot–Agent authentication token |
| `LLM_API_KEY` | Optional LLM provider key |

Use the same strong random `AGENT_INTERNAL_TOKEN` for the Bot and Agent. Never commit `.env` or expose its secrets.

### 2. Start the application

```bash
docker compose up --build -d
```

### 3. Check the services

```bash
docker compose ps
docker compose logs -f
```

## 🤖 Telegram Commands

| Command | Description |
|---|---|
| `/start` | Start the bot |
| `/help` | Show available commands |
| `/addfeed [url]` | Add an RSS/Atom feed |
| `/listfeeds` | List your feeds |
| `/delfeed [id\|url]` | Delete one of your feeds |
| `/news` | View stored news |
| `/delivery` | Toggle automatic delivery |
| `/schedule` | Show your current schedule |
| `/schedule <minutes>` | Set delivery interval |
| `/ask <question>` | Ask about your stored news |
| `/cancel` | Cancel an active input prompt |

## ⏱️ Delivery Schedule

Set your delivery interval in minutes:

```text
/schedule 30
/schedule 60
/schedule 0
```

- Accepts one non-negative integer: `0` to `35,791,394`.
- `0` means no additional per-user delivery delay.
- The default Worker polling interval is **5 minutes**.
- Delivery schedules are checked during Worker polls, not by independent timers.
- `/delivery` controls automatic delivery separately; `/news` and `/ask` remain available when automatic delivery is disabled.

## 📰 RSS Ingestion

When a feed is first processed, the Worker imports **all entries in the feed's current response**. There is no application-level initial-import limit.

- Large feed archives can create a delivery backlog.
- Up to **5 pending articles per user** are processed in each delivery run.
- Duplicate articles are prevented per user using normalized URLs and, when available, external IDs.
- Feed failures use retry backoff and do not automatically stop processing other feeds.

## 💬 AI News Assistant

Use `/ask` to ask questions about news stored from your feeds.

- The Agent retrieves news belonging to the requesting user.
- Answers are based on stored articles, with source titles and links where available.
- The Agent has no web-search or browser tools and does not fetch live news.
- If relevant stored evidence is unavailable, it returns an insufficient-evidence response.
- Retrieval uses keyword matching, not semantic search; some relevant articles may be missed.

Without `LLM_API_KEY`, AI answers are unavailable, but the Worker can use extractive summaries.

## 🔐 Security

- Database queries and constraints enforce per-user data isolation.
- The Agent requires an internal Bearer token for answer requests.
- RSS URLs and redirect destinations are validated against unsafe network targets.
- Article content is treated as untrusted input, with prompt instructions and structural safeguards against prompt injection.
- Source citations are checked against retrieved records.

**Important:** Prompt-injection defenses reduce risk but cannot guarantee that an LLM will never be influenced by malicious content. PostgreSQL Row-Level Security is not configured, and DNS rebinding remains a known URL-validation risk.


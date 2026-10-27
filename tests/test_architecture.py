"""Architecture tests for the three services.

These pin the boundaries the refactor established: which packages may import
which, that the bot never reaches the LLM or the agent's implementation, that the
agent has no way to fetch anything from the web, that the Telegram transport
layer contains no data access, and that importing one service does not drag in
another.

Import checks parse the source with ``ast`` (they need no running service), and
the lazy-import checks run in a fresh interpreter so an already imported module
in this process cannot mask a violation. No network, Docker, or database is
touched.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "ai_rss_news_assistant"

PACKAGE_BOUNDARIES = {
    # The bot talks to the agent over HTTP: it must not import the agent's
    # retrieval/answer implementation or the LLM client at all.
    "bot": (
        "ai_rss_news_assistant.agent",
        "ai_rss_news_assistant.worker",
        "ai_rss_news_assistant.llm",
    ),
    # The agent answers questions only: no Telegram, no ingestion, no delivery.
    "agent": (
        "ai_rss_news_assistant.bot",
        "ai_rss_news_assistant.worker",
        "telegram",
        "feedparser",
    ),
    # The worker ingests, summarizes, and delivers: it never talks to a user or
    # to the agent.
    "worker": (
        "ai_rss_news_assistant.bot",
        "ai_rss_news_assistant.agent",
    ),
}


def _imported_modules(path: Path) -> set[str]:
    """Return the absolute import targets of one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports stay inside the same package by construction.
            modules.add(f"relative:{node.module or ''}")
    return modules


def _package_sources(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


@pytest.mark.parametrize("package", sorted(PACKAGE_BOUNDARIES))
def test_service_packages_respect_their_boundaries(package: str) -> None:
    forbidden = PACKAGE_BOUNDARIES[package]
    violations: list[str] = []
    for path in _package_sources(package):
        for module in _imported_modules(path):
            if any(
                module == prefix or module.startswith(f"{prefix}.")
                for prefix in forbidden
            ):
                violations.append(f"{path.relative_to(SRC)} imports {module}")

    assert violations == []


def test_bot_never_imports_the_llm_client() -> None:
    """The bot must not be able to call a provider, directly or indirectly."""
    sources = {
        path.name: _imported_modules(path) for path in _package_sources("bot")
    }

    assert all(
        "ai_rss_news_assistant.llm" not in modules for modules in sources.values()
    )
    # The client module is the only outbound integration the bot owns.
    assert any(path.name == "agent_client.py" for path in _package_sources("bot"))


def test_agent_has_no_web_fetching_capability() -> None:
    """The agent may only call the provider: no fetching, scraping, or search."""
    forbidden_prefixes = (
        "feedparser",
        "requests",
        "urllib.request",
        "http.client",
        "ai_rss_news_assistant.worker",
        "ai_rss_news_assistant.sqlite",
    )
    offenders: list[str] = []
    for path in _package_sources("agent"):
        for module in _imported_modules(path):
            if any(
                module == prefix or module.startswith(f"{prefix}.")
                for prefix in forbidden_prefixes
            ):
                offenders.append(f"{path.relative_to(SRC)} imports {module}")

    assert offenders == []


def test_chat_client_offers_no_tool_calling() -> None:
    """The provider interface has no tool/function channel to abuse."""
    from ai_rss_news_assistant.llm import ChatClient, OpenAICompatibleClient

    for target in (ChatClient, OpenAICompatibleClient):
        parameters = list(target.complete.__annotations__)  # type: ignore[attr-defined]
        assert "messages" in parameters
        assert not any("tool" in name or "function" in name for name in parameters)


def test_telegram_handlers_contain_no_data_access() -> None:
    """Handlers translate updates; every query happens in the use-case layer."""
    source = (SRC / "bot" / "handlers.py").read_text(encoding="utf-8")

    for needle in ("session.execute", "select(", "session_factory", "IntegrityError"):
        assert needle not in source


def test_use_cases_scope_every_query_by_internal_user_id() -> None:
    """Orchestration resolves a user id and never queries by Telegram identity."""
    source = (SRC / "bot" / "use_cases.py").read_text(encoding="utf-8")

    assert "telegram_user_id" in source
    # Repository calls take the internal id, never the Telegram id.
    for call in (
        "list_feeds(session, user.id)",
        "delete_feed(session, user.id, feed_id)",
        "list_news_items_with_feed(session, user.id, limit=limit)",
    ):
        assert call in source


def test_service_entry_points_are_independently_importable() -> None:
    from ai_rss_news_assistant.agent.entrypoint import run_agent
    from ai_rss_news_assistant.bot.entrypoint import run_bot
    from ai_rss_news_assistant.worker.entrypoint import run_worker

    assert all(callable(entry) for entry in (run_bot, run_worker, run_agent))


def _run_in_fresh_interpreter(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=180,
        check=False,
    )


def test_importing_the_package_imports_no_service() -> None:
    result = _run_in_fresh_interpreter(
        "import sys, ai_rss_news_assistant;"
        "print(sorted(m for m in sys.modules if m.startswith('ai_rss_news_assistant')))"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['ai_rss_news_assistant']"


def test_importing_the_worker_does_not_import_the_bot() -> None:
    result = _run_in_fresh_interpreter(
        "import sys;"
        "import ai_rss_news_assistant.worker.entrypoint;"
        "print('ai_rss_news_assistant.bot' in sys.modules)"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_importing_the_bot_does_not_import_the_agent_implementation() -> None:
    result = _run_in_fresh_interpreter(
        "import sys;"
        "import ai_rss_news_assistant.bot.entrypoint as bot;"
        "print('ai_rss_news_assistant.agent' in sys.modules,"
        " 'ai_rss_news_assistant.llm' in sys.modules)"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"


def test_importing_the_agent_does_not_import_telegram_or_the_bot() -> None:
    result = _run_in_fresh_interpreter(
        "import sys;"
        "import ai_rss_news_assistant.agent.entrypoint;"
        "print('telegram' in sys.modules, 'ai_rss_news_assistant.bot' in sys.modules)"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"


def test_worker_package_is_importable_under_its_own_name() -> None:
    """The historical name collision must not come back.

    ``import ai_rss_news_assistant.worker as worker`` used to bind a function
    and ``import ai_rss_news_assistant.worker.service as service`` used to fail.
    """
    result = _run_in_fresh_interpreter(
        "import ai_rss_news_assistant.worker as worker;"
        "import ai_rss_news_assistant.worker.service as service;"
        "print(type(worker).__name__, hasattr(service, 'NewsWorker'))"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "module True"

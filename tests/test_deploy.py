"""Deployment and configuration regression tests.

These tests are static by default: they read ``compose.yaml``, the
``Dockerfile``, ``.env.example`` and the Alembic scripts and compare them with
the real :mod:`ai_rss_news_assistant.config` model. That catches configuration
drift (an env var added in code but not passed to a service, or the reverse)
without needing Docker at all.

The one test that shells out (``docker compose config``) skips with an explicit
reason when Docker is unavailable. Nothing here starts a container, contacts
Telegram, or calls an LLM provider.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_rss_news_assistant.config import (
    AgentSettings,
    BotSettings,
    Settings,
    WorkerSettings,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "compose.yaml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# Variables a service intentionally does *not* receive. Each entry is a
# decision, not an oversight, so adding a new setting without wiring it makes
# this test fail until the decision is recorded here.
NOT_PASSED_ON_PURPOSE: dict[str, frozenset[str]] = {
    # The bot answers questions over HTTP and never calls a provider itself.
    "bot": frozenset(
        {"LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT_SECONDS"}
    ),
    # The worker never calls the agent and does not authenticate to it.
    "worker": frozenset({"AGENT_INTERNAL_TOKEN"}),
    # The agent never talks to Telegram and never fetches a feed or article.
    "agent": frozenset({"BOT_TOKEN", "FETCH_TIMEOUT_SECONDS"}),
    # Migrations only need the database connection.
    "migrate": frozenset(
        {
            "BOT_TOKEN",
            "LLM_API_KEY",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "LLM_TIMEOUT_SECONDS",
            "AGENT_INTERNAL_TOKEN",
            "FETCH_TIMEOUT_SECONDS",
        }
    ),
}

SETTINGS_BY_SERVICE = {
    "bot": BotSettings,
    "worker": WorkerSettings,
    "agent": AgentSettings,
    "migrate": Settings,
}

ALL_SETTINGS = (Settings, BotSettings, WorkerSettings, AgentSettings)


# --- minimal compose reader -------------------------------------------------


def _compose_services(text: str) -> dict[str, dict[str, str]]:
    """Return ``{service: {ENV_KEY: raw_value}}`` from compose.yaml.

    A full YAML parser would mean adding a dependency for one file, so this
    reads the two structures the tests need: service names at two-space indent
    and their ``environment:`` keys at six-space indent. Anchors, comments and
    other keys are ignored.
    """
    services: dict[str, dict[str, str]] = {}
    in_services = False
    current: str | None = None
    in_environment = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if indent == 0:
            in_services = stripped == "services:"
            current = None
            in_environment = False
            continue

        if not in_services:
            continue

        if indent == 2 and stripped.endswith(":"):
            current = stripped[:-1].strip()
            services[current] = {}
            in_environment = False
            continue

        if current is None:
            continue

        if indent == 4:
            in_environment = stripped == "environment:"
            continue

        if in_environment and indent == 6 and ":" in stripped:
            key, value = stripped.split(":", 1)
            services[current][key.strip()] = value.strip()

    return services


def _settings_field_names(settings_type: type[Settings]) -> set[str]:
    return {name.upper() for name in settings_type.model_fields}


def _env_example_keys() -> set[str]:
    keys: set[str] = set()
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


# --- compose structure ------------------------------------------------------


def test_compose_defines_the_expected_services() -> None:
    services = _compose_services(COMPOSE.read_text(encoding="utf-8"))

    assert set(services) == {"db", "migrate", "bot", "worker", "agent"}


def test_compose_declares_a_persistent_database_volume() -> None:
    text = COMPOSE.read_text(encoding="utf-8")

    assert "db_data:/var/lib/postgresql/data" in text
    assert re.search(r"^volumes:\s*$", text, re.MULTILINE)
    assert re.search(r"^\s{2}db_data:\s*$", text, re.MULTILINE)


def test_database_is_healthy_and_services_wait_for_it() -> None:
    text = COMPOSE.read_text(encoding="utf-8")

    assert "image: postgres:16-alpine" in text
    assert "pg_isready" in text
    # Application services must not start before migrations have been applied.
    assert text.count("condition: service_completed_successfully") == 3
    # The bot additionally waits for the agent's healthcheck.
    assert text.count("condition: service_healthy") >= 3


def test_agent_is_internal_and_publishes_no_host_port() -> None:
    """The agent is reachable on the Compose network only."""
    text = COMPOSE.read_text(encoding="utf-8")
    agent_block = text.split("\n  agent:", 1)[1].split("\n  # One-off", 1)[0]

    assert "ports:" not in agent_block
    assert "healthcheck:" in agent_block


def test_restart_policies_are_set() -> None:
    text = COMPOSE.read_text(encoding="utf-8")

    assert text.count("restart: unless-stopped") == 4  # db + 3 app services
    assert 'restart: "no"' in text  # the one-off migration job


def _service_block(text: str, service: str) -> str:
    match = re.search(
        rf"^  {service}:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL
    )
    assert match is not None, f"compose.yaml has no service {service!r}"
    return match.group(0)


def test_each_service_builds_its_own_dockerfile_target() -> None:
    """A service must never be built from another service's dependency set."""
    text = COMPOSE.read_text(encoding="utf-8")

    for service in ("bot", "worker", "agent", "migrate"):
        block = _service_block(text, service)
        assert "dockerfile: Dockerfile" in block, service
        assert f"target: {service}" in block, service
        assert f"image: ai-rss-news-assistant-{service}:local" in block, service


# --- environment parity -----------------------------------------------------


@pytest.mark.parametrize("service", sorted(SETTINGS_BY_SERVICE))
def test_service_environment_matches_its_settings(service: str) -> None:
    """Every variable a service receives must be a setting it actually reads."""
    services = _compose_services(COMPOSE.read_text(encoding="utf-8"))
    passed = set(services[service])
    known = _settings_field_names(SETTINGS_BY_SERVICE[service])

    unknown = passed - known
    assert unknown == set(), f"{service} receives unknown settings: {sorted(unknown)}"


@pytest.mark.parametrize("service", sorted(SETTINGS_BY_SERVICE))
def test_no_setting_is_silently_left_out_of_a_service(service: str) -> None:
    """Adding a setting without wiring it (or documenting why) fails here."""
    services = _compose_services(COMPOSE.read_text(encoding="utf-8"))
    passed = set(services[service])
    known = _settings_field_names(SETTINGS_BY_SERVICE[service])

    missing = known - passed

    assert missing == set(NOT_PASSED_ON_PURPOSE[service]), (
        f"{service} is missing {sorted(missing)}; either pass it in compose.yaml "
        "or add it to NOT_PASSED_ON_PURPOSE with a reason"
    )


def test_every_setting_is_documented_in_the_env_template() -> None:
    documented = _env_example_keys()

    for settings_type in ALL_SETTINGS:
        missing = _settings_field_names(settings_type) - documented
        assert missing == set(), (
            f"{settings_type.__name__} has undocumented settings: {sorted(missing)}"
        )


def test_env_template_contains_no_real_secret_values() -> None:
    """The template ships placeholders only; a real key must never land here."""
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in {
            "BOT_TOKEN",
            "POSTGRES_PASSWORD",
            "LLM_API_KEY",
            "AGENT_INTERNAL_TOKEN",
            "DATABASE_URL",
        }:
            assert value == "", f"{key} must be empty in .env.example"

    assert "urllib.request" not in ENV_EXAMPLE.read_text(encoding="utf-8")


# --- Dockerfile -------------------------------------------------------------


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _stages(text: str) -> list[str]:
    return re.findall(r"^FROM\s+(\S+)\s+AS\s+(\w+)", text, re.MULTILINE | re.IGNORECASE)


def _stage_text(text: str, name: str) -> str:
    """Return one build stage: from its ``FROM`` line to the next stage."""
    matches = list(
        re.finditer(
            r"^FROM\s+.*\bAS\s+([\w-]+)\s*$", text, re.MULTILINE | re.IGNORECASE
        )
    )
    for index, match in enumerate(matches):
        if match.group(1).lower() == name.lower():
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            return text[match.start() : end]
    raise AssertionError(f"Dockerfile has no stage named {name!r}")


SERVICE_TARGETS = {
    "bot": "deps-bot",
    "worker": "deps-worker",
    "agent": "deps-agent",
    "migrate": "deps-migrate",
}


def test_dockerfile_is_multi_stage() -> None:
    stages = _stages(_dockerfile())

    assert len(stages) >= 2, "the image must be built in more than one stage"
    assert "builder" in {name for _, name in stages}
    assert "runtime" in {name for _, name in stages}


def test_each_service_target_reuses_its_dependency_stage() -> None:
    """Every target copies a venv built from exactly one dependency extra."""
    text = _dockerfile()

    for target, deps_stage in SERVICE_TARGETS.items():
        stage = _stage_text(text, target)
        assert f"COPY --from={deps_stage} /opt/venv /opt/venv" in stage, target

    # Each dependency stage exports the matching extra from pyproject.toml.
    for target, deps_stage in SERVICE_TARGETS.items():
        assert f"--extra {target}" in _stage_text(text, deps_stage), deps_stage


def test_service_stages_do_not_install_build_tools() -> None:
    """No resolver, compiler, or dependency install belongs in a final image."""
    text = _dockerfile()

    for target in SERVICE_TARGETS:
        stage = _stage_text(text, target)
        assert "uv venv" not in stage
        assert "uv pip install" not in stage
        assert "pip install" not in stage


def test_service_stages_run_as_the_non_root_app_user() -> None:
    text = _dockerfile()

    assert re.search(r"uid 10001", text)
    for target in SERVICE_TARGETS:
        stage = _stage_text(text, target)
        assert re.search(r"^USER\s+appuser\s*$", stage, re.MULTILINE), target
        # The source tree is installed into site-packages, not shipped alongside.
        assert "COPY src" not in stage, target


def test_entrypoint_and_default_command_are_preserved() -> None:
    """Each service keeps the ``python -m ai_rss_news_assistant <service>`` contract."""
    text = _dockerfile()
    entrypoint = 'ENTRYPOINT ["python", "-m", "ai_rss_news_assistant"]'

    for target in ("bot", "worker", "agent"):
        stage = _stage_text(text, target)
        assert entrypoint in stage, target
        assert f'CMD ["{target}"]' in stage, target

    # A plain `docker build .` still produces the historical bot image.
    assert text.rstrip().endswith('CMD ["bot"]')
    assert "PYTHONUNBUFFERED=1" in _stage_text(text, "runtime")


def test_alembic_is_available_only_in_the_migration_image() -> None:
    """The one-off migrate service runs the `alembic` console script."""
    text = _dockerfile()
    migrate = _stage_text(text, "migrate")

    assert "COPY alembic.ini" in migrate
    assert "COPY alembic/" in migrate
    assert 'ENTRYPOINT ["alembic"]' in migrate
    assert "/opt/venv/bin" in _stage_text(text, "runtime")  # console scripts on PATH

    for target in ("bot", "worker", "agent"):
        assert "COPY alembic" not in _stage_text(text, target), target


# --- build context ----------------------------------------------------------


def test_dockerignore_excludes_secrets_and_local_state() -> None:
    ignored = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    for required in (".env", ".venv/", ".git/", "**/__pycache__/"):
        assert required in ignored, f"{required} must not reach the build context"


# --- migrations -------------------------------------------------------------


def _migration_graph() -> dict[str, str | None]:
    """Read revision/down_revision straight from the files.

    Importing ``alembic.config`` from the repository root would resolve to the
    local ``alembic/`` script directory, which is not the Alembic package, so
    the graph is read as text instead. The values are plain module-level
    string literals in every migration.
    """
    versions = sorted((REPO_ROOT / "alembic" / "versions").glob("*.py"))
    graph: dict[str, str | None] = {}
    for path in versions:
        text = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision = "([^"]+)"', text, re.MULTILINE)
        down = re.search(r'^down_revision = (None|"([^"]+)")', text, re.MULTILINE)
        assert revision is not None, f"{path.name} has no revision id"
        assert down is not None, f"{path.name} has no down_revision"
        graph[revision.group(1)] = down.group(2)  # None for the base revision
    return graph


def test_alembic_has_a_single_linear_head() -> None:
    graph = _migration_graph()

    assert len(graph) >= 3, f"expected the migration chain, found {sorted(graph)}"

    bases = [revision for revision, down in graph.items() if down is None]
    assert len(bases) == 1, f"expected exactly one base revision, got {bases}"

    heads = set(graph) - {down for down in graph.values() if down}
    # Exactly one head, and every migration that has shipped is still linked
    # into the chain. The newest revision is deliberately not named here: a new
    # migration must not have to edit an unrelated assertion.
    assert len(heads) == 1, f"expected one head, got {sorted(heads)}"
    assert {
        "0001_initial_schema",
        "0002_add_normalized_url",
        "0003_feed_health",
        "0004_delivery_schedule",
        "0005_schema_alignment",
    } <= set(graph)

    # A linear chain: every non-base revision is referenced exactly once.
    parents = [down for down in graph.values() if down]
    assert len(parents) == len(set(parents)) == len(graph) - 1


def test_alembic_env_registers_model_metadata() -> None:
    """Autogenerate must see the models, not an empty metadata object.

    Tables register themselves on ``Base.metadata`` when their module is
    imported, so importing only ``Base`` would make ``alembic check`` report
    every existing table as removed.
    """
    env_py = (REPO_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")

    assert "from ai_rss_news_assistant.db import models" in env_py
    assert "target_metadata = Base.metadata" in env_py


def test_model_columns_are_covered_by_migrations() -> None:
    """Columns added by later features must exist in the migration chain.

    The check is a plain text search over ``alembic/versions``; it catches the
    realistic mistake of editing a model without adding its migration.
    """
    scripts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((REPO_ROOT / "alembic" / "versions").glob("*.py"))
    )

    for column in (
        "normalized_url",
        "consecutive_failures",
        "last_error",
        "next_fetch_at",
        "delivery_interval_seconds",
        "last_delivered_at",
    ):
        assert column in scripts, f"no migration creates {column}"


def test_alembic_env_reads_the_database_url_from_the_environment() -> None:
    """Migrations use the same DATABASE_URL as the application, never a literal."""
    env_py = (REPO_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    ini = (REPO_ROOT / "alembic.ini").read_text(encoding="utf-8")

    assert 'os.environ.get("DATABASE_URL")' in env_py
    # The ini value is a no-op placeholder, not a credential.
    assert "sqlalchemy.url = postgresql+asyncpg://" in ini
    assert "password" not in ini.lower()


# --- docker availability ----------------------------------------------------


@pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not installed in this environment"
)
def test_compose_config_is_valid() -> None:
    """`docker compose config` validates the file without starting anything."""
    env = {
        **os.environ,
        # Supplied so the required-variable guards resolve without reading .env.
        "POSTGRES_PASSWORD": "test-only-not-a-secret",
        "BOT_TOKEN": "test-only-not-a-secret",
    }
    result = subprocess.run(
        ["docker", "compose", "config", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stderr

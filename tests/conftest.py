"""The tests always use a SQLite file in their temporary store, never the DATABASE_URL of a .env or the shell, and
never read the developer's .env (its keys, agent switch and data folders): ENV_FILE points to no file before any
module that loads it (billparse.api) is imported."""

import os
from pathlib import Path

import pytest

os.environ["ENV_FILE"] = str(Path(__file__).parent / "no-such.env")


@pytest.fixture(autouse=True)
def _sqlite_only(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTH", raising=False)
    # the default store (and so the service synonyms' database): the test's own, never the project's store/
    monkeypatch.setenv("PARSER_DATA_DIR", str(tmp_path / "default-store"))
    # pricing assumes only what a test's request gives (the tests of automatic defaults turn them on)
    monkeypatch.setenv("PRICE_AUTO_DEFAULTS", "0")
    # the service catalog: the project's, through links in the test's own folder, so a price load's rebuild (which
    # replaces catalog.msgpack) never writes the project's data/
    project = Path(__file__).resolve().parents[1] / "data"
    own = tmp_path / "catalog"
    own.mkdir()
    for name in ("catalog.msgpack", "raw"):
        if (project / name).exists():
            (own / name).symlink_to(project / name)
    monkeypatch.setenv("CATALOG_DIR", str(own))


@pytest.fixture(autouse=True)
def _inline_workers(monkeypatch):
    """Jobs run in this process (INLINE_WORKER, as with SQLite), never a model request (no key), and every test's
    workers end with the test: none keeps running on a later test's store."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_ENABLED", raising=False)
    monkeypatch.setenv("INLINE_WORKER", "1")
    monkeypatch.setenv("WORKER_POLL_SECONDS", "0.05")
    yield
    from billparse import worker
    worker.stop_inline(wait=30)

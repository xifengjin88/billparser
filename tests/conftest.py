"""The tests always use a SQLite file in their temporary store, never the DATABASE_URL of a .env or the shell, and
never read the developer's .env (its keys, agent switch and data folders): ENV_FILE points to no file before any
module that loads it (billparse.api) is imported."""

import os
from pathlib import Path

import pytest

os.environ["ENV_FILE"] = str(Path(__file__).parent / "no-such.env")


@pytest.fixture(autouse=True)
def _sqlite_only(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTH", raising=False)


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

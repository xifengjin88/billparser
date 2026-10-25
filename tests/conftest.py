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

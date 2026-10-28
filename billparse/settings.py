"""Environment settings: load `.env` once, at the entry points (api, cli).

Lookup order for each variable: the real process environment, then `.env`.
A variable already set in the shell is never overwritten by the file.
`ENV_FILE` points at another file; the default is `.env` in the project root.
See `example.env` for every variable.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> Path | None:
    """Load the env file into os.environ. Returns the file that was read, or None."""
    path = Path(os.environ.get("ENV_FILE", ROOT / ".env"))
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        return None
    load_dotenv(path, override=False)
    return path


def data_dir() -> Path:
    """PARSER_DATA_DIR (default `store`): uploads, result rows, nightly reports, the row model (and the SQLite
    file without DATABASE_URL). A relative path is taken from the project root."""
    path = Path(os.environ.get("PARSER_DATA_DIR", "store"))
    return path if path.is_absolute() else ROOT / path


def catalog_dir() -> Path:
    """CATALOG_DIR (default `data`): the service catalog, its raw inputs and the resolution cache (the learned
    synonyms are in the database: synonyms.py)."""
    path = Path(os.environ.get("CATALOG_DIR", "data"))
    return path if path.is_absolute() else ROOT / path


def configure_logging() -> None:
    """Send `billparse.*` logs to stderr at LOG_LEVEL (default INFO)."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger("billparse")
    logger.setLevel(level)
    if not any(getattr(h, "_billparse", False) for h in logger.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        h._billparse = True  # type: ignore[attr-defined]
        logger.addHandler(h)
        logger.propagate = False

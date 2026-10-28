"""The learned service-name synonyms, in table `service_synonym` (db.ServiceSynonymRow): the resolver's SynonymStore
(svcres.resolver) for billparse.

A synonym maps a normalized label, for one provider or any ('*'), to a catalog service key. Review approval and the
admin's /aliases endpoint add them, several worker processes and the API at once: one statement per change (INSERT ...
ON CONFLICT DO UPDATE, DELETE), never read then write. Each process keeps the synonyms in memory and reads them again
when another process changed them (max(updated) and the count, looked at most every REFRESH_SECONDS), and at once after
its own writes; the resolver then drops its cached picks of the changed labels.

The synonyms were a file, CATALOG_DIR/synonyms.json. On first use, an empty table gets that file's synonyms once (a
journal entry, stream `synonym_import`, records it, so a table emptied later is not filled again). The file is never
changed.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from .db import JournalRow, ServiceSynonymRow, default_url, engine_for
from .settings import data_dir

T = ServiceSynonymRow.__table__
J = JournalRow.__table__
SOURCES = ("review", "admin")
REFRESH_SECONDS = 60.0
IMPORT_STREAM = "synonym_import"
log = logging.getLogger(__name__)


class ServiceSynonymStore:
    """One per database per process (synonym_store())."""

    def __init__(self, engine: sa.Engine, legacy_file: Path | None = None):
        self.engine = engine
        self.legacy_file = legacy_file
        self._lock = threading.Lock()
        self._checked = 0.0
        self._version: tuple | None = None
        self._map: dict[str, str] = {}
        self._imported = False

    # ---------------------------------------------------------------- reading
    def current(self) -> dict[str, str]:
        """Every synonym, "<scope>|<label>" -> service key: a new dict when they changed, else the same one."""
        now = time.monotonic()
        if now - self._checked < REFRESH_SECONDS:
            return self._map
        with self._lock:
            if now - self._checked >= REFRESH_SECONDS:
                if not self._imported:
                    self._import_legacy()
                    self._imported = True
                with self.engine.connect() as c:
                    version = tuple(c.execute(sa.select(sa.func.max(T.c.updated), sa.func.count())).one())
                    if version != self._version:
                        rows = c.execute(sa.select(T.c.scope, T.c.label, T.c.service_key))
                        self._map = {f"{r.scope}|{r.label}": r.service_key for r in rows}
                        self._version = version
                self._checked = now
        return self._map

    def entries(self) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(sa.select(T).order_by(T.c.scope, T.c.label))]

    # ---------------------------------------------------------------- writing
    def add(self, scope: str, label: str, service_key: str, source: str = "admin", by: str | None = None) -> None:
        if source not in SOURCES:
            raise ValueError(f"source {source!r}")
        now = time.time()
        ins = self._insert().values(scope=scope, label=label, service_key=service_key, source=source, created=now,
                                    updated=now, updated_by=by)
        changes = {c: ins.excluded[c] for c in ("service_key", "source", "updated", "updated_by")}
        with self.engine.begin() as c:
            c.execute(ins.on_conflict_do_update(index_elements=[T.c.scope, T.c.label], set_=changes))
        self._checked = 0.0                             # read again on the next lookup

    def remove(self, scope: str, label: str) -> bool:
        with self.engine.begin() as c:
            n = c.execute(T.delete().where((T.c.scope == scope) & (T.c.label == label))).rowcount
        self._checked = 0.0
        return bool(n)

    # ---------------------------------------------------------------- the old file
    def _insert(self):
        return (postgresql.insert(T) if self.engine.dialect.name == "postgresql" else sqlite.insert(T))

    def _import_legacy(self) -> None:
        """An empty table, never imported into: take the synonyms of the old file. Two processes at once: the rows
        conflict and are skipped (ON CONFLICT DO NOTHING); at worst the import is recorded twice."""
        path = self.legacy_file
        if path is None or not path.is_file():
            return
        with self.engine.begin() as c:
            if c.execute(sa.select(sa.func.count()).select_from(T)).scalar() or \
                    c.execute(sa.select(J.c.id).where(J.c.stream == IMPORT_STREAM).limit(1)).first():
                return
            now = time.time()
            rows = [{"scope": scope, "label": label, "service_key": key, "source": "review", "created": now,
                     "updated": now, "updated_by": None}
                    for k, key in json.loads(path.read_text()).items()
                    for scope, sep, label in [k.partition("|")] if sep and isinstance(key, str)]
            if rows:
                c.execute(self._insert().values(rows).on_conflict_do_nothing(index_elements=[T.c.scope, T.c.label]))
            c.execute(J.insert().values(stream=IMPORT_STREAM, ts=now, data={"file": str(path), "synonyms": len(rows)}))
        log.info("imported %d service synonyms from %s", len(rows), path)


_STORES: dict[str, ServiceSynonymStore] = {}
_LOCK = threading.Lock()


def synonym_store(catalog_dir: Path, url: str | None = None) -> ServiceSynonymStore:
    """The synonym store of this database (DATABASE_URL, else the SQLite file in PARSER_DATA_DIR), one per process. An
    empty table first takes the synonyms of <catalog_dir>/synonyms.json."""
    key = url or default_url(data_dir())
    with _LOCK:
        if key not in _STORES:
            if key.startswith("sqlite:///"):                # its folder, as Store makes it
                Path(key.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
            _STORES[key] = ServiceSynonymStore(engine_for(key), Path(catalog_dir) / "synonyms.json")
        return _STORES[key]

"""The header dictionary: column headers that mean a canonical field, beyond the mapper's English rules.

Two sources, learned first:

- the learned dictionary, table `header_synonym` (db.HeaderSynonymRow): headers confirmed by an approved review, a
  verified spec or an admin, and headers that must NOT map to a field (rejected). In the database, because several
  worker processes learn at once; one statement per learn (INSERT ... ON CONFLICT DO UPDATE), never read then write.
- the seed, `data/header_synonyms.seed.json` (in git): foreign and English headers per field, written by hand.

A learned entry counts once confirmed by review or an admin, or twice by specs: it scores 3.5. A seed entry scores
3.0. A header that two fields claim (in the learned entries, or else in the seed) is ambiguous: no hit. The mapper
(mapper.map_columns) adds a hit as a candidate even when no rule matches the header, and never maps a rejected pair.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from .db import HeaderSynonymRow
from .transforms import CANONICAL

T = HeaderSynonymRow.__table__
SEED_PATH = Path(__file__).resolve().parents[1] / "data" / "header_synonyms.seed.json"
LEARNED_SCORE = 3.5
SEED_SCORE = 3.0
SOURCES = ("review", "spec", "admin")
NEVER_LEARNED = {"coverage", "provider"}      # derived (coverage.apply) or too vague to learn from one header
REFRESH_SECONDS = 60.0
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def normalize(header: object) -> str:
    """The dictionary's key of a header: camelCase split, case and accents folded, every run of punctuation one
    space. For an ASCII header it is layout.norm_h of the split header ("UnitPrice" -> "unit price"); letters of
    other scripts are kept ("単価", "Quantité" -> "quantite")."""
    text = unicodedata.normalize("NFKD", CAMEL.sub(" ", str(header)))
    out = []
    for ch in text:                                 # drop the accents of Latin letters only (not a kana's mark)
        if unicodedata.combining(ch) and out and out[-1].isascii():
            continue
        out.append(ch)
    text = unicodedata.normalize("NFKC", "".join(out)).casefold()
    return re.sub(r"[\W_]+", " ", text).strip()


def learnable(header: object, field: str) -> bool:
    """Never learned: the fields coverage and provider, a header of one or two characters, a number."""
    h = normalize(header)
    return field in CANONICAL and field not in NEVER_LEARNED and len(h.replace(" ", "")) > 2 \
        and not h.replace(" ", "").isdigit()


def load_seed(path: Path = SEED_PATH) -> dict[str, set[str]]:
    """normalized header -> the fields the seed gives it (more than one: ambiguous, never a hit)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for field, langs in raw.items():
        if field.startswith("_"):
            continue
        if field not in CANONICAL:
            raise ValueError(f"{path.name}: {field} is not a canonical field")
        for headers in langs.values():
            for h in headers:
                out.setdefault(normalize(h), set()).add(field)
    return out


class HeaderDictionary:
    """One per database per process (dictionary()): the learned entries are read again when another process changed
    them (max(updated) and the count, looked at most every REFRESH_SECONDS), and at once after this one's writes."""

    def __init__(self, engine: sa.Engine, seed: dict[str, set[str]] | None = None):
        self.engine = engine
        self.seed = load_seed() if seed is None else seed
        self._lock = threading.Lock()
        self._checked = 0.0
        self._version: tuple | None = None
        self._hits: dict[str, set[str]] = {}         # normalized header -> learned fields that count
        self._rejected: set[tuple[str, str]] = set()

    # ---------------------------------------------------------------- reading
    def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._checked < REFRESH_SECONDS:
            return
        with self._lock:
            if now - self._checked < REFRESH_SECONDS:
                return
            with self.engine.connect() as c:
                version = tuple(c.execute(sa.select(sa.func.max(T.c.updated), sa.func.count())).one())
                if version != self._version:
                    hits: dict[str, set[str]] = {}
                    rejected: set[tuple[str, str]] = set()
                    for r in c.execute(sa.select(T.c.header_norm, T.c.field, T.c.count, T.c.source, T.c.rejected)):
                        if r.rejected:
                            rejected.add((r.header_norm, r.field))
                        elif r.source in ("review", "admin") or r.count >= 2:
                            hits.setdefault(r.header_norm, set()).add(r.field)
                    self._hits, self._rejected, self._version = hits, rejected, version
            self._checked = now

    def lookup(self, header: object) -> tuple[str, float] | None:
        """(field, score) for a header: a learned field (3.5), else a seed field (3.0); None when it has none, or two
        (ambiguous), or its one field is rejected for it."""
        self._refresh()
        h = normalize(header)
        for fields, score in ((self._hits.get(h), LEARNED_SCORE), (self.seed.get(h), SEED_SCORE)):
            ok = {f for f in fields or () if (h, f) not in self._rejected}
            if fields:
                return (next(iter(ok)), score) if len(ok) == 1 and len(fields) == 1 else None
        return None

    def rejected(self, header: object, field: str) -> bool:
        self._refresh()
        return (normalize(header), field) in self._rejected

    def entries(self, field: str | None = None, source: str | None = None, rejected: bool | None = None) -> list[dict]:
        q = sa.select(T).order_by(T.c.field, T.c.header_norm)
        if field:
            q = q.where(T.c.field == field)
        if source:
            q = q.where(T.c.source == source)
        if rejected is not None:
            q = q.where(T.c.rejected == rejected)
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(q)]

    # ---------------------------------------------------------------- writing
    def _upsert(self, header: object, field: str, source: str, by: str | None, rejected: bool, count: int) -> None:
        now = time.time()
        ins = (postgresql.insert(T) if self.engine.dialect.name == "postgresql" else sqlite.insert(T)).values(
            header_norm=normalize(header), field=field, count=count, source=source, rejected=rejected, created=now,
            updated=now, updated_by=by)
        changes = {"source": ins.excluded.source, "updated": ins.excluded.updated,
                   "updated_by": ins.excluded.updated_by}
        if rejected:
            changes["rejected"] = sa.true()
        else:                                           # a confirmation counts; an admin's also lifts a rejection
            changes["count"] = T.c.count + 1
            if source == "admin":
                changes["rejected"] = sa.false()
        with self.engine.begin() as c:
            c.execute(ins.on_conflict_do_update(index_elements=[T.c.header_norm, T.c.field], set_=changes))
        self._checked = 0.0                             # read again on the next lookup

    def learn(self, header: object, field: str, source: str, by: str | None = None) -> bool:
        """Count one confirmation of header -> field. False (nothing written) for a field or header never learned."""
        if source not in SOURCES:
            raise ValueError(f"source {source!r}")
        if not learnable(header, field):
            return False
        self._upsert(header, field, source, by, rejected=False, count=1)
        return True

    def reject(self, header: object, field: str, by: str | None = None, source: str = "admin") -> bool:
        """header must not map to field: no rule, seed entry or detector maps it there any more."""
        if field not in CANONICAL or not normalize(header):
            return False
        self._upsert(header, field, source, by, rejected=True, count=0)
        return True

    def remove(self, header: object, field: str) -> bool:
        """Forget an entry (a learned header or a rejection). header: as printed or normalized."""
        with self.engine.begin() as c:
            n = c.execute(T.delete().where((T.c.header_norm == normalize(header)) & (T.c.field == field))).rowcount
        self._checked = 0.0
        return bool(n)


_DICTIONARIES: dict[str, HeaderDictionary] = {}
_LOCK = threading.Lock()


def dictionary(engine: sa.Engine) -> HeaderDictionary:
    """The header dictionary of this database (one per process)."""
    key = str(engine.url)
    with _LOCK:
        if key not in _DICTIONARIES:
            _DICTIONARIES[key] = HeaderDictionary(engine)
        return _DICTIONARIES[key]


# ---------------------------------------------------------------- what specs teach
def spec_headers(spec: dict | None) -> dict[str, str]:
    """field -> the header a spec reads it from (its first `from`): the file's own columns, never the interpreter's
    (`_group`, `_id`) or a constant."""
    out = {}
    for field, c in ((spec or {}).get("columns") or {}).items():
        src = (c or {}).get("from") or []
        if "const" in (c or {}) or not src or str(src[0]).startswith("_"):
            continue
        out[field] = str(src[0])
    return out


def learn_spec(engine: sa.Engine, spec: dict | None, source: str, by: str | None = None) -> list[tuple[str, str]]:
    """Learn the headers of a spec that a person approved (review) or that became verified (spec). Built-in specs
    teach nothing: their headers are known."""
    if not spec or spec.get("trust") == "builtin":
        return []
    d = dictionary(engine)
    return [(h, f) for f, h in spec_headers(spec).items() if d.learn(h, f, source, by)]


def reject_remapped(engine: sa.Engine, old: dict | None, new: dict | None, by: str | None = None) \
        -> list[tuple[str, str]]:
    """A repaired spec that reads a header as another field than the version before: that header is rejected for
    the old field (source spec; an admin can remove it)."""
    before = {(h, f) for f, h in spec_headers(old).items()}
    after = {(h, f) for f, h in spec_headers(new).items()}
    read = {h for h, _ in after}
    d = dictionary(engine)
    return sorted((h, f) for h, f in before - after if h in read and d.reject(h, f, by, source="spec"))

"""Resolve raw service strings or product codes to canonical services.

Cascade, stopping at the first hit:
    1. resolution cache   (everything resolved before, incl. reviewed LLM picks)
    2. product/service code exact match
    3. synonyms           (learned from human corrections; a SynonymStore keeps them)
    4. exact normalized name
    5. fuzzy match        (RapidFuzz; auto-accept above a high cutoff)
    6. fallback hook      (e.g. an LLM choosing ONLY from fuzzy candidates)

Always call resolve_many() with the unique values of a column, not row by row.

Synonyms live in a SynonymStore: a JSON file (JsonSynonymStore, svcres on its own, the default) or a database table
that several processes share (billparse.synonyms). The resolver asks the store for its current synonyms on every
resolve; when they changed (here or in another process), it drops the cached picks of the changed labels.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from rapidfuzz import fuzz, process

from .catalog import PROVIDERS, Catalog
from .normalize import azure_head, code_key, norm, norm_loose

log = logging.getLogger(__name__)

CATALOG_KEY = "__catalog__"


@dataclass
class Resolution:
    raw: str
    service_key: str | None
    service: str | None = None
    service_code: str | None = None                  # the provider's id: AmazonEC2, an Azure serviceId, a GCP id
    provider: str | None = None
    category: str | None = None
    method: str = "unresolved"
    score: float = 0.0
    candidates: list[tuple[str, float]] | None = None  # for review UI when unresolved


# fallback(raw, provider_hint, candidates) -> service_key or None
Fallback = Callable[[str, str | None, list[tuple[str, float]]], str | None]


class SynonymStore(Protocol):
    """Where the learned synonyms live. A synonym's key is "<scope>|<label>": scope '*' or a provider, label the
    normalized label (normalize.norm)."""

    def current(self) -> dict[str, str]:
        """Every synonym, key -> service key. The same object until the synonyms change (the resolver compares it by
        identity); never changed in place."""
        ...

    def add(self, scope: str, label: str, service_key: str, source: str = "admin", by: str | None = None) -> None:
        """Set one synonym (a new one, or a new service for its label)."""
        ...

    def remove(self, scope: str, label: str) -> bool:
        """Forget one synonym. False when there was none."""
        ...

    def entries(self) -> list[dict]:
        """Every synonym with its record: scope, label, service_key, source, created, updated, updated_by."""
        ...


class JsonSynonymStore:
    """The synonyms in one JSON file (svcres on its own): read once, written whole on each change. One process only."""

    def __init__(self, path: Path):
        self.path = path
        self._map: dict[str, str] = _read(path)

    def current(self) -> dict[str, str]:
        return self._map

    def add(self, scope: str, label: str, service_key: str, source: str = "admin", by: str | None = None) -> None:
        self._write({**self._map, f"{scope}|{label}": service_key})

    def remove(self, scope: str, label: str) -> bool:
        key = f"{scope}|{label}"
        if key not in self._map:
            return False
        self._write({k: v for k, v in self._map.items() if k != key})
        return True

    def entries(self) -> list[dict]:
        t = self.path.stat().st_mtime if self.path.exists() else None
        return [{"scope": k.partition("|")[0], "label": k.partition("|")[2], "service_key": v, "source": None,
                 "created": t, "updated": t, "updated_by": None} for k, v in sorted(self._map.items())]

    def _write(self, synonyms: dict[str, str]) -> None:
        _write_json(self.path, json.dumps(synonyms, indent=1, sort_keys=True))
        self._map = synonyms


class Resolver:
    def __init__(self, catalog: Catalog, data_dir: Path,
                 fuzzy_accept: float = 92.0, fuzzy_candidates: float = 70.0,
                 fallback: Fallback | None = None, synonyms: SynonymStore | None = None):
        self.cat = catalog
        self.fuzzy_accept = fuzzy_accept
        self.fuzzy_candidates = fuzzy_candidates
        self.fallback = fallback
        self.store: SynonymStore = synonyms if synonyms is not None else JsonSynonymStore(data_dir / "synonyms.json")
        self.cache_path = data_dir / "resolution_cache.json"
        self._syn: dict[str, str] = self.store.current()
        self.cache: dict[str, dict] = _read_cache(self.cache_path)
        self._dirty = False
        # fuzzy and prefix picks hold only for the catalog they were made with: a new catalog can have a better
        # exact match, so a rebuilt catalog starts a new cache (reviewed picks are synonyms, kept apart)
        if self.cache.get(CATALOG_KEY) != {"built_at": catalog.built_at}:
            self.cache, self._dirty = {CATALOG_KEY: {"built_at": catalog.built_at}}, True
        # Fuzzy choice lists per provider. Plain lists are much faster for RapidFuzz.
        self._small = _split_by_provider(self.cat.names)      # hundreds per provider
        self._large = _split_by_provider(self.cat.products)   # tens of thousands (Azure)

    # ---------- public API ----------
    @property
    def synonyms(self) -> dict[str, str]:
        """The current synonyms, "<scope>|<label>" -> service key (read only)."""
        self._sync()
        return self._syn

    def resolve_many(self, values: Iterable[object], provider: str | None = None,
                     dominant_share: float = 0.8) -> dict[str, Resolution]:
        """Resolve unique values. Without a provider hint, a second pass retries
        unresolved/ambiguous values using the file's dominant provider, if one
        provider accounts for >= dominant_share of the confident matches."""
        out: dict[str, Resolution] = {}
        for v in values:
            if v is None or (isinstance(v, float) and v != v):  # None / NaN
                continue
            s = str(v).strip()
            if s and s not in out:
                out[s] = self.resolve(s, provider)

        if provider is None:
            dom = self.dominant_provider(out.values(), dominant_share)
            if dom:
                for s, r in out.items():
                    if not r.service_key:
                        retry = self.resolve(s, dom)
                        if retry.service_key:
                            retry.method += "+inferred_provider"
                            out[s] = retry
        self.flush()
        return out

    @staticmethod
    def dominant_provider(results: Iterable[Resolution], share: float = 0.8) -> str | None:
        counts: dict[str, int] = {}
        for r in results:
            if r.service_key and r.method in ("code", "name", "synonym", "prefix"):
                counts[r.provider] = counts.get(r.provider, 0) + 1
        total = sum(counts.values())
        if total >= 3:
            p, n = max(counts.items(), key=lambda kv: kv[1])
            if n / total >= share:
                return p
        return None

    def resolve(self, raw: str, provider: str | None = None) -> Resolution:
        provs = [provider] if provider else list(PROVIDERS)
        ck = f"{provider or '*'}|{norm(raw)}"
        self._sync()

        r = self.cache.get(ck)
        if r and r["service_key"] in self.cat.services:  # skip entries made stale by a rebuild
            # a pick cached before a synonym covered its label (by another process, which wrote the cache file
            # this one read) gives way to the synonym, as add_synonym would have dropped it
            if self._synonym(raw, provs) is None and self._synonym_prefix(raw, provs) is None:
                return self._make(raw, r["service_key"], "cache:" + r["method"], r["score"])
            del self.cache[ck]
            self._dirty = True

        # 2. codes
        for p in provs:
            if (key := self.cat.codes.get(f"{p}:{code_key(raw)}")):
                return self._remember(ck, self._make(raw, key, "code", 100))

        # 3. synonyms (provider-specific first, then global)
        if (key := self._synonym(raw, provs)):
            return self._remember(ck, self._make(raw, key, "synonym", 100))

        # 4. exact normalized name, then the loose name (prefixes and filler words removed) only when the
        #    exact one has no hit: "Microsoft Fabric" is not "Service Fabric". If one key matches different
        #    services in different providers ("Kubernetes"), it's ambiguous: return candidates, don't guess.
        for n in (norm(raw), norm_loose(raw)):
            hits = {key for p in provs for table in (self.cat.names, self.cat.products)
                    if (key := table.get(f"{p}:{n}"))}
            if len(hits) == 1:
                return self._remember(ck, self._make(raw, hits.pop(), "name", 100))
            if len(hits) > 1:
                return Resolution(raw=raw, service_key=None, method="ambiguous",
                                  candidates=[(k, 100.0) for k in sorted(hits)])
            # a name known to fit several services (a shared Azure meter name, a service family): no guess
            if (amb := next((self.cat.ambiguous[f"{p}:{n}"] for p in provs
                             if f"{p}:{n}" in self.cat.ambiguous), None)):
                return Resolution(raw=raw, service_key=None, method="ambiguous",
                                  candidates=[(k, 100.0) for k in amb[:5]])

        # 4a. a person's alias as a word prefix: an alias "Veeam" covers "Veeam Veeam Data Cloud Vault"
        if (key := self._synonym_prefix(raw, provs)):
            return self._remember(ck, self._make(raw, key, "synonym-prefix", 95))

        # 4b. longest word prefix that is a known name: "Amazon EC2 m5.xlarge" -> "ec2",
        #     "Virtual Machines D4s v5 East US" -> "virtual machines"
        words = norm_loose(raw).split()
        for k in range(min(5, len(words) - 1), 0, -1):
            pre = " ".join(words[:k])
            hits = {key for p in provs for table in (self.cat.names, self.cat.products)
                    if (key := table.get(f"{p}:{pre}"))}
            if len(hits) == 1:
                return self._remember(ck, self._make(raw, hits.pop(), "prefix", 95))
            if len(hits) > 1:
                break

        # 4c. a Microsoft invoice product name ("Az DDOS Prot-NW Prot Plan-1 Hr"): its service part, short words
        #     written out, as a name and then as a word prefix ("azure ddos protection")
        azure = provider == "azure" or (provider is None and re.match(r"\s*az(?:\s|sql)", raw, re.I))
        if azure and (head := azure_head(raw)) and head != norm(raw):
            for n in dict.fromkeys((head, norm_loose(head))):
                if (key := self.cat.names.get(f"azure:{n}") or self.cat.products.get(f"azure:{n}")):
                    return self._remember(ck, self._make(raw, key, "abbrev", 95))
            hw = norm_loose(head).split()
            for k in range(min(5, len(hw)), 0, -1):
                if (key := self.cat.names.get(f"azure:{' '.join(hw[:k])}")):
                    return self._remember(ck, self._make(raw, key, "abbrev", 90))

        # 5. fuzzy
        cands = self._fuzzy(raw, provs)
        if cands and cands[0][1] >= self.fuzzy_accept:
            return self._remember(ck, self._make(raw, cands[0][0], "fuzzy", cands[0][1]))

        # 6. fallback (LLM etc.), constrained to candidates
        if self.fallback and cands:
            key = self.fallback(raw, provider, cands)
            if key and key in self.cat.services:
                return self._remember(ck, self._make(raw, key, "fallback", 0))

        res = Resolution(raw=raw, service_key=None, candidates=cands[:5])
        return res

    def add_synonym(self, raw: str, service_key: str, provider: str | None = None, source: str = "admin",
                    by: str | None = None) -> None:
        """Record a human correction (source review or admin, by whom). Takes effect immediately."""
        if service_key not in self.cat.services:
            raise KeyError(f"Unknown service key: {service_key}")
        n = norm(raw)
        self.store.add(provider or "*", n, service_key, source, by)
        self._sync()
        self._drop_cached({n})                         # also when the synonym was already there
        self._dirty = True
        self.flush()

    def remove_synonym(self, provider: str | None, label_norm: str) -> bool:
        """Forget a correction (its key as add_synonym wrote it: provider or '*', and the normalized label)."""
        removed = self.store.remove(provider or "*", label_norm)
        self._sync()
        return removed

    def key_for_code(self, code: str, provider: str | None = None) -> str | None:
        """The service with this code (AmazonEC2, an Azure serviceId or productId, a GCP id), or None."""
        for p in [provider] if provider else list(PROVIDERS):
            if (key := self.cat.codes.get(f"{p}:{code_key(code)}")):
                return key
        return next((k for k, s in self.cat.services.items()
                     if s.get("code") == code and (not provider or s["provider"] == provider)), None)

    def flush(self) -> None:
        if self._dirty:
            _write_json(self.cache_path, json.dumps(self.cache))
            self._dirty = False

    # ---------- internals ----------
    def _sync(self) -> None:
        """Take the store's current synonyms; when they changed, drop the cached picks of the changed labels."""
        syn = self.store.current()
        if syn is self._syn:
            return
        old, self._syn = self._syn, syn
        self._drop_cached({k.partition("|")[2] for k in old.keys() ^ syn.keys()}
                          | {k.partition("|")[2] for k in old.keys() & syn.keys() if old[k] != syn[k]})

    def _drop_cached(self, labels: set[str]) -> None:
        """Drop the cached picks of these labels, and of longer labels they cover as a word prefix (step 4a)."""
        if not labels:
            return
        stale = []
        for k in self.cache:
            words = k.partition("|")[2].split()
            if k != CATALOG_KEY and any(" ".join(words[:i]) in labels for i in range(1, len(words) + 1)):
                stale.append(k)
        for k in stale:
            del self.cache[k]
        self._dirty = self._dirty or bool(stale)

    def _synonym(self, raw: str, provs: list[str]) -> str | None:
        n = norm(raw)
        return next((key for sk in [*(f"{p}|{n}" for p in provs), f"*|{n}"] if (key := self._syn.get(sk))), None)

    def _synonym_prefix(self, raw: str, provs: list[str]) -> str | None:
        words = norm(raw).split()
        for k in range(min(6, len(words) - 1), 0, -1):
            pre = " ".join(words[:k])
            for sk in [*(f"{p}|{pre}" for p in provs), f"*|{pre}"]:
                if (key := self._syn.get(sk)):
                    return key
        return None

    def _fuzzy(self, raw: str, provs: list[str]) -> list[tuple[str, float]]:
        q = norm_loose(raw)
        if not q:
            return []
        best: dict[str, float] = {}

        def score(names: list[str], keys: list[str]) -> None:
            for _, sc, idx in process.extract(q, names, scorer=fuzz.WRatio, processor=None,
                                              limit=10, score_cutoff=self.fuzzy_candidates):
                best[keys[idx]] = max(best.get(keys[idx], 0), sc)

        for p in provs:
            score(*self._small[p])                      # canonical names + aliases: fast
        if not best or max(best.values()) < self.fuzzy_accept:
            for p in provs:
                names, keys = self._large[p]
                if not names:
                    continue
                # cheap prefilter with plain ratio, then rescore the top 200 with WRatio
                pre = process.extract(q, names, scorer=fuzz.ratio, processor=None, limit=200)
                score([names[i] for _, _, i in pre], [keys[i] for _, _, i in pre])
        return sorted(best.items(), key=lambda kv: -kv[1])[:10]

    def _make(self, raw: str, key: str, method: str, score: float) -> Resolution:
        s = self.cat.services.get(key, {})
        return Resolution(raw=raw, service_key=key, service=s.get("name"), service_code=s.get("code"),
                          provider=s.get("provider"), category=s.get("category"),
                          method=method, score=round(float(score), 1))

    def _remember(self, ck: str, res: Resolution) -> Resolution:
        # codes and exact names are cheap to recompute; cache the expensive ones
        if res.method in ("fuzzy", "fallback", "prefix"):
            self.cache[ck] = {"service_key": res.service_key, "method": res.method, "score": res.score}
            self._dirty = True
        return res


def _split_by_provider(table: dict[str, str]) -> dict[str, tuple[list[str], list[str]]]:
    seen: dict[str, dict[str, str]] = {p: {} for p in PROVIDERS}
    for k, svc in table.items():
        p, n = k.split(":", 1)
        seen[p].setdefault(n, svc)
    return {p: (list(d.keys()), list(d.values())) for p, d in seen.items()}


def _read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _read_cache(path: Path) -> dict:
    """The resolution cache, or empty when the file is unreadable: it is only a cache, rebuilt as values resolve."""
    try:
        return _read(path)
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable resolution cache %s: %s", path, e)
        return {}


def _write_json(path: Path, text: str) -> None:
    """Write a whole file or nothing: several workers share these files, and a reader must never see half of one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def to_dict(r: Resolution) -> dict:
    return asdict(r)

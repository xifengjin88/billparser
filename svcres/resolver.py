"""Resolve raw service strings or product codes to canonical services.

Cascade, stopping at the first hit:
    1. resolution cache   (everything resolved before, incl. reviewed LLM picks)
    2. product/service code exact match
    3. synonyms           (learned from human corrections)
    4. exact normalized name
    5. fuzzy match        (RapidFuzz; auto-accept above a high cutoff)
    6. fallback hook      (e.g. an LLM choosing ONLY from fuzzy candidates)

Always call resolve_many() with the unique values of a column, not row by row.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

from .catalog import PROVIDERS, Catalog
from .normalize import code_key, norm, norm_loose


@dataclass
class Resolution:
    raw: str
    service_key: str | None
    service: str | None = None
    provider: str | None = None
    category: str | None = None
    method: str = "unresolved"
    score: float = 0.0
    candidates: list[tuple[str, float]] | None = None  # for review UI when unresolved


# fallback(raw, provider_hint, candidates) -> service_key or None
Fallback = Callable[[str, str | None, list[tuple[str, float]]], str | None]


class Resolver:
    def __init__(self, catalog: Catalog, data_dir: Path,
                 fuzzy_accept: float = 92.0, fuzzy_candidates: float = 70.0,
                 fallback: Fallback | None = None):
        self.cat = catalog
        self.fuzzy_accept = fuzzy_accept
        self.fuzzy_candidates = fuzzy_candidates
        self.fallback = fallback
        self.syn_path = data_dir / "synonyms.json"
        self.cache_path = data_dir / "resolution_cache.json"
        self.synonyms: dict[str, str] = _read(self.syn_path)
        self.cache: dict[str, dict] = _read(self.cache_path)
        self._dirty = False
        # Fuzzy choice lists per provider. Plain lists are much faster for RapidFuzz.
        self._small = _split_by_provider(self.cat.names)      # hundreds per provider
        self._large = _split_by_provider(self.cat.products)   # tens of thousands (Azure)

    # ---------- public API ----------
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

        r = self.cache.get(ck)
        if r and r["service_key"] in self.cat.services:  # skip entries made stale by a rebuild
            return self._make(raw, r["service_key"], "cache:" + r["method"], r["score"])

        # 2. codes
        for p in provs:
            if (key := self.cat.codes.get(f"{p}:{code_key(raw)}")):
                return self._remember(ck, self._make(raw, key, "code", 100))

        # 3. synonyms (provider-specific first, then global)
        for sk in [*(f"{p}|{norm(raw)}" for p in provs), f"*|{norm(raw)}"]:
            if (key := self.synonyms.get(sk)):
                return self._remember(ck, self._make(raw, key, "synonym", 100))

        # 4. exact normalized name. If it matches different services in different
        #    providers ("Kubernetes"), it's ambiguous: return candidates, don't guess.
        hits = {key for p in provs for n in (norm(raw), norm_loose(raw))
                for table in (self.cat.names, self.cat.products)
                if (key := table.get(f"{p}:{n}"))}
        if len(hits) == 1:
            return self._remember(ck, self._make(raw, hits.pop(), "name", 100))
        if len(hits) > 1:
            return Resolution(raw=raw, service_key=None, method="ambiguous",
                              candidates=[(k, 100.0) for k in sorted(hits)])

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

    def add_synonym(self, raw: str, service_key: str, provider: str | None = None) -> None:
        """Record a human correction. Takes effect immediately."""
        if service_key not in self.cat.services:
            raise KeyError(f"Unknown service key: {service_key}")
        self.synonyms[f"{provider or '*'}|{norm(raw)}"] = service_key
        # drop stale cache entries for this string
        n = norm(raw)
        for k in [k for k in self.cache if k.endswith(f"|{n}")]:
            del self.cache[k]
        self.syn_path.write_text(json.dumps(self.synonyms, indent=1, sort_keys=True))
        self._dirty = True
        self.flush()

    def flush(self) -> None:
        if self._dirty:
            self.cache_path.write_text(json.dumps(self.cache))
            self._dirty = False

    # ---------- internals ----------
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
        return Resolution(raw=raw, service_key=key, service=s.get("name"),
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


def to_dict(r: Resolution) -> dict:
    return asdict(r)

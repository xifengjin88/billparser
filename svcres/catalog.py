"""Compact in-memory service catalog.

Built from the providers' own lists, saved as msgpack, loaded once per worker (again when the file changes). Two
sources, the first one winning where both name a service or product:
    db    rows the caller read from its price catalog (billparse.service_catalog: rebuilt after every price load),
          adding what the JSON lacks; a service already known keeps its name (its key, which synonyms point to)
          per provider: Azure products (productId, productName, serviceName, serviceId, serviceFamily), AWS service
          codes and names, GCP service IDs and names
    raw   the fetch scripts' JSON (scripts/fetch_*.py into raw_dir): the fallback for what the price catalog lacks
          (a provider whose prices are not loaded, Azure services not downloaded)
The seeds (AWS categories and aliases, Azure aliases, Marketplace sellers) are added to both.

Structure (all plain dicts, so lookups are O(1)):
    services: service_key -> {provider, name, category, family}
    codes:    "provider:CODE" -> service_key        (product codes, service codes, service IDs)
    names:    "provider:normalized name" -> service_key   (canonical names + aliases, small)
    products: "provider:normalized name" -> service_key   (Azure product names, large)

Names and product names are kept apart so fuzzy search can scan the small list
first and only fall back to the large one with a cheap prefilter.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import msgpack

from .normalize import code_key, norm, norm_loose
from .seeds import AWS_SEED, AZURE_ALIASES, AZURE_FAMILY_TO_CATEGORY, GCP_KEYWORD_CATEGORY, MARKETPLACE_SELLERS

PROVIDERS = ("azure", "aws", "gcp")


@dataclass
class Catalog:
    services: dict[str, dict] = field(default_factory=dict)
    codes: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    products: dict[str, str] = field(default_factory=dict)
    built_at: float = 0.0
    sources: dict[str, dict[str, int]] = field(default_factory=dict)   # provider -> {"db": n, "raw": n} entries

    # ---------- building ----------
    def add_service(self, provider: str, name: str, category: str, family: str | None = None,
                    code: str | None = None) -> str:
        """code: the provider's own id of the service (AWS service code, Azure serviceId, GCP service id)."""
        key = f"{provider}:{name}"
        svc = self.services.setdefault(key, {"provider": provider, "name": name,
                                             "category": category, "family": family, "code": None})
        if code and not svc.get("code"):
            svc["code"] = code
        self.add_name(provider, name, key)
        return key

    def add_code(self, provider: str, code: str, key: str) -> None:
        if code:
            self.codes[f"{provider}:{code_key(code)}"] = key

    def add_name(self, provider: str, name: str, key: str, product: bool = False) -> None:
        target = self.products if product else self.names
        for n in {norm(name), norm_loose(name)}:
            if n:
                target.setdefault(f"{provider}:{n}", key)

    # ---------- persistence ----------
    def save(self, path: Path) -> None:
        blob = {"services": self.services, "codes": self.codes, "names": self.names,
                "products": self.products, "built_at": self.built_at, "sources": self.sources}
        tmp = path.with_name(path.name + ".tmp")           # whole or not at all: workers load it while it changes
        tmp.write_bytes(msgpack.packb(blob))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Catalog:
        blob = msgpack.unpackb(path.read_bytes())
        return cls(**blob)

    def stats(self) -> dict:
        by_p = {p: sum(1 for s in self.services.values() if s["provider"] == p) for p in PROVIDERS}
        return {"services": by_p, "codes": len(self.codes), "names": len(self.names),
                "product_names": len(self.products), "sources": self.sources}


def build(raw_dir: Path, db: dict[str, list[dict]] | None = None) -> Catalog:
    """db: provider -> rows from the price catalog (see the module doc); they win over raw_dir's JSON."""
    db = db or {}
    cat = Catalog(built_at=time.time())
    _build_azure(cat, raw_dir / "azure_products.json", db.get("azure") or [])
    _azure_aliases(cat)
    _build_aws(cat, raw_dir / "aws_services.json", db.get("aws") or [])
    _build_gcp(cat, raw_dir / "gcp_services.json", db.get("gcp") or [])
    for provider in PROVIDERS:                          # third-party products: one Marketplace service per cloud
        key = cat.add_service(provider, "Marketplace", "Marketplace", code="Marketplace")
        cat.add_code(provider, "Marketplace", key)
        for seller in MARKETPLACE_SELLERS.get(provider, []):
            cat.add_name(provider, seller, key)
    return cat


def _load(path: Path) -> dict:
    if not path.exists():
        print(f"  (missing {path.name}, skipping)")
        return {}
    return json.loads(path.read_text())


def _build_azure(cat: Catalog, path: Path, rows: list[dict]) -> None:
    """rows: productId, productName, serviceName, serviceId, serviceFamily (the price catalog's)."""
    products = _load(path)
    raw = len(products)
    for r in rows:
        if r.get("productId") and r.get("serviceName"):
            products[r["productId"]] = r
    cat.sources["azure"] = {"db": len({r.get("productId") for r in rows if r.get("productId")}),
                            "raw": raw}
    for pid, p in products.items():
        svc = p.get("serviceName")
        if not svc:
            continue
        fam = p.get("serviceFamily") or "Other"
        key = cat.add_service("azure", svc, AZURE_FAMILY_TO_CATEGORY.get(fam, "Other"), fam, p.get("serviceId"))
        cat.add_code("azure", pid, key)
        if p.get("serviceId"):
            cat.add_code("azure", p["serviceId"], key)
        # Product names ("Virtual Machines Dv3 Series") are what invoices often show.
        if p.get("productName"):
            cat.add_name("azure", p["productName"], key, product=True)


def _azure_aliases(cat: Catalog) -> None:
    for name, aliases in AZURE_ALIASES.items():
        key = f"azure:{name}"
        if key in cat.services:
            for a in aliases:
                cat.add_name("azure", a, key)


def _build_aws(cat: Catalog, path: Path, rows: list[dict]) -> None:
    """rows: code and name (the price files' serviceCode and serviceName), the most used name first."""
    raw = _load(path)
    cat.sources["aws"] = {"db": len({r["code"] for r in rows if r.get("code")}), "raw": len(raw)}
    db_names: dict[str, list[str]] = {}
    for r in rows:
        if r.get("code") and r.get("name") and r["name"] not in db_names.setdefault(r["code"], []):
            db_names[r["code"]].append(r["name"])
    for code, names in db_names.items():
        # a known service keeps its display name: it is the service's key ("aws:Amazon Elastic Compute Cloud"),
        # which the learned synonyms point to; the price catalog's names are added to it, and a new code gets them
        old = (raw.get(code) or {}).get("names") or []
        raw[code] = {"names": old + [n for n in names if n not in old]}
    codes = set(raw) | set(AWS_SEED)
    for code in sorted(codes):
        names = (raw.get(code) or {}).get("names") or []
        category, aliases = AWS_SEED.get(code, ("Other", []))
        display = names[0] if names else (aliases[0] if aliases else code)
        key = cat.add_service("aws", display, category, code=code)
        cat.add_code("aws", code, key)
        for n in [code, *names[1:], *aliases]:
            cat.add_name("aws", n, key)


def _build_gcp(cat: Catalog, path: Path, rows: list[dict]) -> None:
    """rows: code (the service ID) and name (its display name)."""
    services = _load(path)
    cat.sources["gcp"] = {"db": len({r["code"] for r in rows if r.get("code") and r.get("name")}), "raw": len(services)}
    for r in rows:
        if r.get("code") and r.get("name"):
            services[r["code"]] = {"displayName": r["name"]}
    for sid, s in services.items():
        name = s.get("displayName")
        if not name:
            continue
        n = norm(name)
        category = next((c for kw, c in GCP_KEYWORD_CATEGORY if kw in n), "Other")
        key = cat.add_service("gcp", name, category, code=sid)
        cat.add_code("gcp", sid, key)

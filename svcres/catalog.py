"""Compact in-memory service catalog.

Built nightly from the raw fetch outputs, saved as msgpack, loaded once per worker.

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
from .seeds import AWS_SEED, AZURE_FAMILY_TO_CATEGORY, GCP_KEYWORD_CATEGORY

PROVIDERS = ("azure", "aws", "gcp")


@dataclass
class Catalog:
    services: dict[str, dict] = field(default_factory=dict)
    codes: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    products: dict[str, str] = field(default_factory=dict)
    built_at: float = 0.0

    # ---------- building ----------
    def add_service(self, provider: str, name: str, category: str, family: str | None = None) -> str:
        key = f"{provider}:{name}"
        self.services.setdefault(key, {"provider": provider, "name": name,
                                       "category": category, "family": family})
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
                "products": self.products, "built_at": self.built_at}
        path.write_bytes(msgpack.packb(blob))

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        blob = msgpack.unpackb(path.read_bytes())
        return cls(**blob)

    def stats(self) -> dict:
        by_p = {p: sum(1 for s in self.services.values() if s["provider"] == p) for p in PROVIDERS}
        return {"services": by_p, "codes": len(self.codes), "names": len(self.names),
                "product_names": len(self.products)}


def build(raw_dir: Path) -> Catalog:
    cat = Catalog(built_at=time.time())
    _build_azure(cat, raw_dir / "azure_products.json")
    _build_aws(cat, raw_dir / "aws_services.json")
    _build_gcp(cat, raw_dir / "gcp_services.json")
    return cat


def _load(path: Path) -> dict:
    if not path.exists():
        print(f"  (missing {path.name}, skipping)")
        return {}
    return json.loads(path.read_text())


def _build_azure(cat: Catalog, path: Path) -> None:
    for pid, p in _load(path).items():
        svc = p.get("serviceName")
        if not svc:
            continue
        fam = p.get("serviceFamily") or "Other"
        key = cat.add_service("azure", svc, AZURE_FAMILY_TO_CATEGORY.get(fam, "Other"), fam)
        cat.add_code("azure", pid, key)
        if p.get("serviceId"):
            cat.add_code("azure", p["serviceId"], key)
        # Product names ("Virtual Machines Dv3 Series") are what invoices often show.
        if p.get("productName"):
            cat.add_name("azure", p["productName"], key, product=True)


def _build_aws(cat: Catalog, path: Path) -> None:
    raw = _load(path)
    codes = set(raw) | set(AWS_SEED)
    for code in sorted(codes):
        names = (raw.get(code) or {}).get("names") or []
        category, aliases = AWS_SEED.get(code, ("Other", []))
        display = names[0] if names else (aliases[0] if aliases else code)
        key = cat.add_service("aws", display, category)
        cat.add_code("aws", code, key)
        for n in [code, *names[1:], *aliases]:
            cat.add_name("aws", n, key)


def _build_gcp(cat: Catalog, path: Path) -> None:
    for sid, s in _load(path).items():
        name = s.get("displayName")
        if not name:
            continue
        n = norm(name)
        category = next((c for kw, c in GCP_KEYWORD_CATEGORY if kw in n), "Other")
        key = cat.add_service("gcp", name, category)
        cat.add_code("gcp", sid, key)

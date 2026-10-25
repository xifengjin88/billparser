"""Give each line of a tree (tree.py) its context: account, service, region, charge type, provider.

The labels above a line are its path ("Total for this invoice" > "Acme Prod (123456789012)" > "Amazon Elastic
Compute Cloud" > "Charges"). Which label is which comes from the spec's "context" options, with neutral defaults:

  account       a label with an account id: a 12-digit AWS account or an Azure GUID by default (option "account",
                a regex with the named groups id and name)
  region        a label that is a region heading (option "region", a regex); by default a label that is a whole
                AWS, Azure or GCP region name or code (svcres.regions). The line's region is the code
                ("ca-central-1"); the printed name ("Canada (Central)") is kept as region_name
  service       the label nearest the line that the service catalog resolves (code, synonym, name or prefix match);
                labels that match option "not_service" are skipped
  charge type   the first of option "charge_types" ([[name, regex], ...]) that matches the line's label, then the
                labels above it. By default: Tax when the column of the line's amount is a tax column ("Taxes") or
                the line or a label above it names a tax ("Total tax", "GST"; not "pre-tax"); Credit when the amount
                is negative and a credit, refund or discount is named; Purchase (a commitment fee) when a savings
                plan, reservation or commitment fee is named. Each stays its own line, with its service.
  cells         a line printed in cells (a PDF table row) gives more: the first cell is its description, a cell
                that is a number and a unit ("588 Hrs") its quantity and unit, and its other amounts are kept by
                their column names ("Pre-tax charges: 4,045.98")
  label_split   a separator for labels printed in two languages ("AWS Lambda / AWS Lambda"): the first part is used.
                By default " / " when most labels of the bill have that form
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from svcres.regions import lookup as region_lookup

from .analyze import RESOLVER_LOCK

ACCOUNT = r"^(?P<name>.*?)\s*\(?(?P<id>\d{12})\)?\s*$"
GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
PROVIDER_WORDS = [("aws", r"amazon web services|\baws\b|amazon"), ("azure", r"microsoft|\bazure\b"),
                  ("gcp", r"google cloud|\bgcp\b")]
SERVICE_METHODS = ("code", "synonym", "name", "prefix")
# a tax, not an amount before or after tax ("Total pre-tax", "Post-tax charges", "excl. tax")
TAX = re.compile(r"(?<!pre-)(?<!pre )(?<!post-)(?<!post )(?<!excl\. )(?<!excluding )(?<!before )(?<!after )"
                 r"\b(tax|taxes|sales tax|vat|gst|hst|pst|qst|consumption tax)\b(?!\s*(?:incl|included|excl))", re.I)
CREDIT = re.compile(r"\b(credits?|refunds?|discounts?|promotional|promotion)\b", re.I)
# the fee of a commitment, not the usage it covers ("Savings Plan covered usage", "... per hour (Reserved Instance)")
COMMITMENT_FEE = re.compile(r"\b(savings plans?|reserved instances?|reservations?|commitments?|\bCUD|\bRI)\b.*"
                            r"\b(fees?|upfront|recurring|purchase)\b|\b(upfront|recurring|commitment) fees?\b", re.I)
QUANTITY = re.compile(r"^(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?:\s+(\S.*))?$")
OPTIONS = {"account", "region", "not_service", "charge_types", "label_split"}


def check_context(d: dict) -> list[str]:
    """What is wrong with a spec's context options (for the spec checker)."""
    if not isinstance(d, dict):
        return ["context must be an object"]
    out = [f"Unknown context option {k!r}; known: {', '.join(sorted(OPTIONS))}" for k in d if k not in OPTIONS]
    for k in ("account", "region", "not_service"):
        if d.get(k) is not None:
            try:
                rx = re.compile(d[k])
            except (re.error, TypeError) as e:
                out.append(f"context.{k} is not a valid regex: {e}")
                continue
            if k == "account" and "id" not in rx.groupindex:
                out.append("context.account needs a named group 'id' (and may have 'name')")
    for item in d.get("charge_types") or []:
        if not (isinstance(item, list) and len(item) == 2 and all(isinstance(x, str) for x in item)):
            out.append("context.charge_types must be a list of [name, regex] pairs")
            continue
        try:
            re.compile(item[1])
        except re.error as e:
            out.append(f"context.charge_types regex for {item[0]!r} is not valid: {e}")
    if d.get("label_split") is not None and not isinstance(d["label_split"], str):
        out.append("context.label_split must be a string")
    return out


class Context:
    def __init__(self, options: dict | None = None):
        o = options or {}
        self.account = re.compile(o.get("account") or ACCOUNT, re.I)
        self.region = re.compile(o["region"], re.I) if o.get("region") else None
        self.not_service = re.compile(o["not_service"], re.I) if o.get("not_service") else None
        self.charge_types = [(name, re.compile(rx, re.I)) for name, rx in (o.get("charge_types") or [])]
        self.split = o.get("label_split")

    def label(self, text: str) -> str:
        if self.split:
            parts = [p.strip() for p in text.split(self.split)]
            if len(parts) >= 2 and parts[0]:
                return parts[0]
        return text.strip()

    def account_of(self, label: str) -> tuple[str | None, str | None]:
        m = self.account.search(label.strip())
        if m and m.groupdict().get("id"):
            name = (m.groupdict().get("name") or "").strip(" -:()")
            return m["id"], (name or None)
        g = GUID.search(label)
        return (g.group(0), label.replace(g.group(0), "").strip(" -:()") or None) if g else (None, None)

    def region_of(self, label: str) -> str | None:
        if self.region:
            return label.strip() if self.region.search(label.strip()) else None
        return label.strip() if region_lookup(label) else None

    def region_code_of(self, label: str) -> str | None:
        hit = region_lookup(label)
        return hit[1] if hit else None

    def charge_type_of(self, label: str, path: list[str], column: str | None = None,
                       amount: float | None = None) -> str | None:
        if not self.charge_types:                       # the defaults: tax, credit, commitment fee; else usage
            texts = [column or "", label, *path]
            if any(TAX.search(t) for t in texts):
                return "Tax"
            # a credit takes money off ("T3 CPU Credits" is usage: it costs money)
            if amount is not None and amount < 0 and any(CREDIT.search(t) for t in texts):
                return "Credit"
            if any(COMMITMENT_FEE.search(t) for t in texts):
                return "Purchase"
            return None
        for text in [label, *reversed(path)]:
            t = self.label(text)
            for name, rx in self.charge_types:
                if rx.search(t):
                    return name
        return None

    def skip(self, label: str) -> bool:
        return bool(self.region_of(label) or self.account_of(label)[0]
                    or (self.not_service and self.not_service.search(label)))


def provider_of(texts: list[str]) -> str | None:
    blob = " ".join(texts[:200]).lower()
    counts = {p: len(re.findall(rx, blob)) for p, rx in PROVIDER_WORDS}
    best = max(counts, key=counts.get)
    return best if counts[best] else None


def lines_frame(lines: list[dict], catalog_dir: Path | None, currency: str | None,
                options: dict | None = None) -> pd.DataFrame:
    """The tree's lines as canonical rows: description, billed_cost and the context fields."""
    if not lines:
        return pd.DataFrame(columns=["description", "billed_cost", "_src", "_path"])
    ctx = Context(options)
    if ctx.split is None and _bilingual(lines):        # "Charges / Frais", "AWS Lambda / AWS Lambda"
        ctx.split = " / "
    resolver = _resolver(catalog_dir)
    provider = provider_of([p for line in lines[:300] for p in line["path"]] + [line["label"] for line in lines[:300]])
    cache: dict[str, str | None] = {}

    def service(label: str) -> str | None:
        k = ctx.label(label)
        if k not in cache:
            cache[k] = None
            if resolver is not None and len(k) >= 2 and not re.fullmatch(r"[\d\W]+", k):
                with RESOLVER_LOCK:
                    r = resolver.resolve(k, provider)
                if r.service_key and r.method.split(":")[-1] in SERVICE_METHODS:
                    cache[k] = k
        return cache[k]

    out = []
    for n, line in enumerate(lines):
        path = line["path"]
        rec = {"_line": n, "description": ctx.label(line["label"]), "billed_cost": line["amount"], "_src": line["src"],
               "_path": " > ".join(ctx.label(p) for p in path) or None}
        where = line.get("path_src") or {}             # where each heading is printed (for the cites)
        for p in reversed(path):                        # nearest first
            acct, name = ctx.account_of(p)
            if acct and "account_id" not in rec:
                rec["account_id"], rec["account_name"] = acct, name
                rec["_account_src"] = where.get(p)
            reg = ctx.region_of(ctx.label(p))
            if reg and "region" not in rec:              # the region code ("ca-central-1"); the printed name beside it
                rec["region"] = ctx.region_code_of(reg) or reg
                rec["region_name"] = reg
                rec["_region_src"] = where.get(p)
        extra = list(line.get("extra") or [])
        column = _cells(rec, line, extra)               # first: a line in cells has a cleaner description
        rec["description"] = ctx.label(rec["description"])
        for text in [rec["description"], *reversed(path)]:   # the nearest label the catalog knows
            if ctx.skip(ctx.label(text)):
                continue
            s = service(text)
            if s:
                rec["service_raw"] = s
                rec["_service_src"] = where.get(text, line["src"])
                break
        rec["charge_type"] = ctx.charge_type_of(rec["description"], path, column, line["amount"])
        for t in list(extra):                           # a part number and a period printed with the line
            if t.startswith("Part number: "):
                rec["product_code"] = t.split(": ", 1)[1]
                extra.remove(t)
            elif "charge_start" not in rec and (p := _period(t)):
                rec["charge_start"], rec["charge_end"] = p
                extra.remove(t)
        if provider:
            rec["provider"] = provider
        if currency:
            rec["currency"] = currency
        if extra:
            rec["_extra"] = " | ".join(extra)
        tax = rec.pop("_tax", None)
        out.append(rec)
        if tax is not None and abs(tax) >= 0.005:       # the line's tax: a line of its own, with the same service
            out.append({**{k: v for k, v in rec.items() if k not in ("quantity", "unit", "unit_price")},
                        "billed_cost": tax, "charge_type": "Tax"})
    df = pd.DataFrame(out)
    if "service_raw" not in df:
        df["service_raw"] = None
    # a line with no catalog service keeps its nearest heading that is not an account, a region or a skipped label
    miss = df["service_raw"].isna()
    if miss.any():
        df.loc[miss, "service_raw"] = [_nearest_heading(lines[n]["path"], ctx) for n in df.loc[miss, "_line"]]
    return df.drop(columns="_line")


UNIT_PRICE = re.compile(r"\bunit\s*price\b|^price\b", re.I)
WORD_UNIT = re.compile(r"[A-Za-z][\w/-]*")
DATE = re.compile(r"\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{2,4}|\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}/\d{1,2}/\d{4}")


def _cells(rec: dict, line: dict, extra: list[str]) -> str | None:
    """What a line printed in cells adds: description, quantity and unit, unit price, its other amounts by column
    name, and a Total that is a net charge plus its tax (rec["_tax"]: the tax, made a line of its own). Returns the
    column name of the line's own amount."""
    from .tree import money_in
    cells, columns = line.get("cells") or [], line.get("columns") or {}
    if len(cells) < 2:
        return None
    whole = line["label"].startswith(cells[0])          # the row's own text (not a name joined from other rows)
    if whole and re.search(r"[A-Za-z]{2}", cells[0]):   # words, not an amount alone (a price inside is fine)
        rec["description"] = cells[0]
    rest = cells[1:] if whole else [c for c in cells if money_in(c) or not re.search(r"[A-Za-z]{2}", c)]
    for c in rest:
        q = None if money_in(c) else QUANTITY.match(c)
        if q and "quantity" not in rec:
            unit = (q.group(2) or "").strip()
            if unit.endswith("-") and extra:            # the unit wrapped onto the next line
                unit += extra.pop(0)
            elif not unit and extra and WORD_UNIT.fullmatch(extra[0]):
                unit = extra.pop(0)
            rec["quantity"] = float(q.group(1).replace(",", ""))
            if unit:
                rec["unit"] = unit
    named = list(columns.items()) if columns else [(None, c) for c in rest]
    values = [(name, money_in(text)[0][0]) for name, text in named if len(money_in(text)) == 1]
    amount = line.get("amount")
    main = next((name for name, v in reversed(values) if amount is not None and abs(v - amount) < 0.005), None)
    used = {main}
    for name, v in values:
        if name and name not in used and UNIT_PRICE.search(name):
            rec.setdefault("unit_price", v)
            used.add(name)
    tax = next(((n, v) for n, v in values if n and n not in used and TAX.search(n)), None)
    if main and tax and re.search(r"\btotal\b", main, re.I):     # Total = net charge + tax: two lines
        net = next(((n, v) for n, v in values if n and n not in used | {tax[0]} and abs(v + tax[1] - amount) < 0.011),
                   None)
        if net:
            rec["billed_cost"], rec["_tax"] = net[1], tax[1]
            used |= {tax[0], net[0]}
    for name, v in values:
        if name and name not in used:
            extra.append(f"{name}: {v:.2f}")
    return main


def _period(text: str) -> tuple[str, str] | None:
    """A charge period at the start of a text ("01-Jul-2025 - 31-Jul-2025", "04/25/2025-05/20/2025 4.33 2")."""
    m = re.match(rf"\s*({DATE.pattern})\s*(?:-|–|to)\s*({DATE.pattern})(?:\s|$)", text)
    if not m:
        return None
    days = [pd.to_datetime(d, errors="coerce", dayfirst=False) for d in (m.group(1), m.group(2))]
    if any(pd.isna(d) for d in days):
        return None
    return days[0].strftime("%Y-%m-%d"), days[1].strftime("%Y-%m-%d")


def _bilingual(lines: list[dict]) -> bool:
    """A bill printed in two languages: most of its labels are "first language / second language"."""
    labels = {t for line in lines for t in [line["label"], *line["path"]] if t}
    two = sum(1 for t in labels if len([p for p in t.split(" / ") if p.strip()]) == 2)
    return len(labels) >= 4 and two >= 0.5 * len(labels)


def _nearest_heading(path: list[str], ctx: Context) -> str | None:
    for p in reversed(path):
        if not ctx.skip(ctx.label(p)):
            return ctx.label(p)
    return None


def _resolver(catalog_dir: Path | None):
    if catalog_dir is None:
        return None
    from .analyze import get_resolver
    with RESOLVER_LOCK:
        return get_resolver(Path(catalog_dir))

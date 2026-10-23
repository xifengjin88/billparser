"""LLM calls. Only two jobs:

1. generate_spec: write a JSON spec from a redacted preview, using similar verified
   specs as examples, and fix it from validator feedback (repair loop).
2. extract_rows: read rows directly from a block when no spec works. The pipeline then
   derives a deterministic spec from those rows (see induce.py).

The LLM never sees whole files and never writes code.
"""

from __future__ import annotations

import json
import os
import re
from typing import Protocol

import requests

from .transforms import CANONICAL, TRANSFORMS

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = os.environ.get("BILLPARSE_MODEL", "claude-sonnet-5-5")


class LLMUnavailable(RuntimeError):
    pass


class LLM(Protocol):
    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str: ...


class AnthropicLLM:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY not set")
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        r = requests.post(API_URL, timeout=120, headers={
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self.model, "max_tokens": max_tokens, "system": system,
                  "messages": [{"role": "user", "content": user}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])


def default_llm() -> LLM | None:
    try:
        return AnthropicLLM()
    except LLMUnavailable:
        return None


# ---------------------------------------------------------------- redaction
_REDACT = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<EMAIL>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<GUID>"),
    (re.compile(r"\b\d{12}\b"), "<ACCOUNT12>"),
    (re.compile(r"\b[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}\b"), "<GCP_BILLING_ACCT>"),
    (re.compile(r"arn:aws[\w:/\-.*]+"), "<ARN>"),
    (re.compile(r"/subscriptions/[^\s,]+", re.I), "<AZURE_RESOURCE>"),
]


def redact(text: str) -> str:
    for rx, token in _REDACT:
        text = rx.sub(token, text)
    return text


def render_preview(preview: dict) -> str:
    """preview: {"file_type", "regions": [{"name","kind","rows": [[...]], "bold_rows": [...]}]}"""
    out = [f"file_type: {preview['file_type']}"]
    for reg in preview["regions"]:
        out.append(f"\n## region {reg['name']} ({reg['kind']})")
        for i, row in enumerate(reg["rows"]):
            flag = " [bold]" if i in reg.get("bold_rows", []) else ""
            out.append(f"{i:>3}{flag}: " + " | ".join(str(c) for c in row))
    return redact("\n".join(out))


# ---------------------------------------------------------------- spec generation
SPEC_GUIDE = f"""You write JSON parsing specs for cloud bills. A trusted interpreter runs them.

Spec keys (use only these):
- "source": "frame" (CSV/JSON/Parquet column tables), "grid" (Excel sheets / PDF tables), or "lines" (regex per text line)
- "detect": {{"required": [header names that identify this layout]}}
- "meta": {{"provider": "aws|azure|gcp|other", "kind": "cost_lines", "cost_basis": "billed|amortized"}}
- "table" (grid only): {{"sheet": regex, "start": regex of header row, "end": regex where table ends,
   "header_rows": 1|2, "multi_page": true, "group_header": {{"carry_to": "_group"}}, "continuation": "merge"|"drop",
   "stop_at_total": true}}
- "columns": {{canonical_field: {{"from": [source header names], "t": [transforms], "coalesce": bool, "const": value}}}}
   Special sources: "_group" (carried group header text), "_id"/"_var"/"_value" (after unpivot)
- "derive": {{canonical_field: "expression"}} using + - * / and coalesce(), abs(), round(), fillna()
- "reshape": {{"unpivot": {{"id": header|"first", "exclude": regex, "total_row": regex}}}}
- "filters": [{{"drop_if_blank": field}}, {{"drop_if_match": {{"field": f, "regex": r}}}}]
- "fields": {{name: {{"anchor": regex, "take": "right_of"|"below", "regex": capture regex}}}}
- "lines": [{{"pattern": regex with named groups matching canonical fields}}]
- "totals": {{"from_fields": ["invoice_total"]}}

Canonical fields: {json.dumps(CANONICAL)}
Transforms: {sorted(TRANSFORMS)} (e.g. "number", {{"date": "%m/%d/%Y"}}, {{"strip_suffix": "($)"}}, {{"json_sum": "amount"}})

Rules: map billed_cost (required) and service_raw or description. Never put totals, subtotals or tax
lines into cost rows unless they are line items. Return ONLY the JSON object."""


def _json_from(text: str) -> dict:
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0:
        raise ValueError("No JSON object in LLM response")
    return json.loads(text[start:end + 1])


def generate_spec(llm: LLM, preview: dict, examples: list[tuple[str, dict]],
                  feedback: list[str] | None = None, previous: dict | None = None) -> dict:
    parts = []
    for i, (ex_preview, ex_spec) in enumerate(examples, 1):
        parts.append(f"<example {i}>\nPREVIEW:\n{ex_preview}\nSPEC:\n{json.dumps(ex_spec)}\n</example {i}>")
    parts.append(f"PREVIEW of the new file:\n{render_preview(preview)}")
    if previous and feedback:
        parts.append("Your previous spec:\n" + json.dumps(previous))
        parts.append("Validation problems to fix:\n- " + "\n- ".join(feedback))
    parts.append("Write the spec.")
    return _json_from(llm.complete(SPEC_GUIDE, "\n\n".join(parts)))


# ---------------------------------------------------------------- direct extraction
EXTRACT_GUIDE = """Extract every billable line item from this cloud bill text into JSON.
Return {"rows": [{"service_raw": str, "description": str|null, "billed_cost": number,
"quantity": number|null, "unit": str|null, "unit_price": number|null}], "total": number|null}.
billed_cost exactly as printed (negative for credits). Include tax lines as rows with service_raw "Tax".
Do not include subtotal or total lines as rows. Put the invoice grand total in "total".
Return ONLY JSON."""


def extract_rows(llm: LLM, preview: dict) -> dict:
    return _json_from(llm.complete(EXTRACT_GUIDE, render_preview(preview), max_tokens=8000))

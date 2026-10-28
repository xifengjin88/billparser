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
import threading
from typing import Protocol

import requests

from .spec_schema import rows_json_schema, spec_json_schema
from .transforms import CANONICAL, TRANSFORMS

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = os.environ.get("BILLPARSE_MODEL", "claude-sonnet-5-5")
# USD per million tokens (input, output): estimates for limits and run costs, not a bill
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
          "claude-fable-5-1": (10.0, 50.0)}


def cost_of(usage: dict, model: str) -> float:
    """The cost of one call from its usage (cache writes at the input price, cache reads at a tenth of it)."""
    pin, pout = PRICES.get(model, PRICES["claude-opus-5-5"])
    tokens_in = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) \
        + 0.1 * (usage.get("cache_read_input_tokens") or 0)
    return (tokens_in * pin + (usage.get("output_tokens") or 0) * pout) / 1e6


# The model and effort choices the upload page lists (Backend: the spec route; Fallback: the agent), "model:effort".
DEFAULT_CHOICES = "haiku-5-5:low,haiku-5-5:medium,sonnet-5-5:medium,sonnet-5-5:high,opus-5-5:medium,opus-5-5:high"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def llm_choices() -> list[str]:
    """LLM_CHOICES from the environment (comma separated), each a valid choice."""
    raw = os.environ.get("LLM_CHOICES") or DEFAULT_CHOICES
    return [c.strip() for c in raw.split(",") if c.strip() and parse_choice(c.strip()) is not None]


def parse_choice(choice: str | None) -> tuple[str, str | None] | None:
    """'sonnet-5-5:high' -> ('claude-sonnet-5-5', 'high'); 'opus-5-5' -> ('claude-opus-5-5', None). None for the
    defaults ('', 'claude') and for text that is not a known model with a valid effort."""
    if not choice or choice in ("claude", "none"):
        return None
    model, _, effort = choice.partition(":")
    model = model.strip() if model.strip().startswith("claude-") else f"claude-{model.strip()}"
    effort = effort.strip() or None
    if model not in PRICES or (effort is not None and effort not in EFFORTS):
        return None
    return model, effort


class LLMUnavailable(RuntimeError):
    pass


class LLM(Protocol):
    """complete(): text. An LLM may also have complete_json(system, user, schema, max_tokens) -> dict: the answer as a
    whole JSON object for the schema (generate_spec and extract_rows use it when it is there)."""
    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str: ...


ANSWER_TOOL = "answer"
ANSWER_NOTE = (f"\n\nGive your answer by calling the {ANSWER_TOOL} tool once, with the whole answer as its input; "
               "write nothing else.")


class AnthropicLLM:
    """The spec and extraction calls. Settings (env): BILLPARSE_MODEL, BILLPARSE_EFFORT (low | medium | high | xhigh |
    max; empty: the model's default), BILLPARSE_MAX_TOKENS (the least output allowed: thinking counts in it),
    BILLPARSE_LLM_TIMEOUT (seconds). What the calls cost is counted per thread (parses run in parallel)."""

    def __init__(self, api_key: str | None = None, model: str | None = None, effort: str | None = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY not set")
        self.model = model or os.environ.get("BILLPARSE_MODEL") or DEFAULT_MODEL
        self.effort = effort or (os.environ.get("BILLPARSE_EFFORT") or "").strip() or None
        self.max_tokens = int(os.environ.get("BILLPARSE_MAX_TOKENS") or 16000)
        self.timeout = float(os.environ.get("BILLPARSE_LLM_TIMEOUT") or 300)
        self._local = threading.local()

    def spent(self) -> float:
        """USD spent by this thread's calls so far."""
        return getattr(self._local, "cost", 0.0)

    def charge(self, usd: float) -> None:
        """Count cost spent in another thread (a spec candidate generated in parallel) as this thread's."""
        self._local.cost = self.spent() + usd

    def _post(self, body: dict) -> dict:
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        r = requests.post(API_URL, timeout=self.timeout, json=body, headers={
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        r.raise_for_status()
        out = r.json()
        self.charge(cost_of(out.get("usage") or {}, out.get("model") or self.model))
        return out

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        out = self._post({"model": self.model, "max_tokens": max(max_tokens, self.max_tokens), "system": system,
                          "messages": [{"role": "user", "content": user}]})
        return "".join(b.get("text", "") for b in out["content"] if b.get("type") == "text")

    def complete_json(self, system: str, user: str, schema: dict, max_tokens: int = 4000) -> dict:
        """The answer as one tool's input: a whole JSON object, shaped by schema. tool_choice is "auto" and the prompt
        names the tool (the current Opus and Sonnet models refuse a forced tool_choice, and output_config.format
        needs additionalProperties false on every object, which a spec's free-form options cannot have). An answer
        in text instead is read as before; one cut off by max_tokens raises ValueError (a repair round)."""
        out = self._post({"model": self.model, "max_tokens": max(max_tokens, self.max_tokens),
                          "system": system + ANSWER_NOTE, "messages": [{"role": "user", "content": user}],
                          "tools": [{"name": ANSWER_TOOL, "description": "Your answer.", "input_schema": schema}],
                          "tool_choice": {"type": "auto"}})
        if out.get("stop_reason") == "max_tokens":
            raise ValueError("The answer was cut off (max_tokens)")
        for b in out["content"]:
            if b.get("type") == "tool_use" and b.get("name") == ANSWER_TOOL and isinstance(b.get("input"), dict):
                return b["input"]
        return _json_from("".join(b.get("text", "") for b in out["content"] if b.get("type") == "text"))


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


def _ask(llm: LLM, system: str, user: str, schema: dict, max_tokens: int = 4000) -> dict:
    """The answer as JSON: schema-shaped when the LLM has complete_json, else read from its text."""
    if hasattr(llm, "complete_json"):
        return llm.complete_json(system, user, schema, max_tokens=max_tokens)
    return _json_from(llm.complete(system, user, max_tokens=max_tokens))


def generate_spec(llm: LLM, preview: dict, examples: list[tuple[str, dict]],
                  feedback: list[str] | None = None, previous: dict | None = None, note: str | None = None,
                  settled: dict[str, str] | None = None) -> dict:
    """note: a word to this candidate only (parallel candidates are told to try another reading). settled: the
    columns the rule mapper is sure of (field -> header): the spec keeps them and maps only the others."""
    parts = []
    for i, (ex_preview, ex_spec) in enumerate(examples, 1):
        parts.append(f"<example {i}>\nPREVIEW:\n{ex_preview}\nSPEC:\n{json.dumps(ex_spec)}\n</example {i}>")
    parts.append(f"PREVIEW of the new file:\n{render_preview(preview)}")
    if settled:
        parts.append(settled_block(settled))
    if previous and feedback:
        parts.append("Your previous spec:\n" + json.dumps(previous))
        parts.append("Validation problems to fix:\n- " + "\n- ".join(feedback))
    if note:
        parts.append(note)
    parts.append("Write the spec.")
    return _ask(llm, SPEC_GUIDE, "\n\n".join(parts), spec_json_schema())


def settled_block(settled: dict[str, str]) -> str:
    lines = "\n".join(f"{f} <- {h}" for f, h in sorted(settled.items()))
    return ("Settled columns (the rule mapper read these from their headers and values; keep each one in "
            "\"columns\" as given, and map only the other columns):\n" + lines)


# ---------------------------------------------------------------- direct extraction
EXTRACT_GUIDE = """Extract every billable line item from this cloud bill text into JSON.
Return {"rows": [{"service_raw": str, "description": str|null, "billed_cost": number,
"quantity": number|null, "unit": str|null, "unit_price": number|null,
"charge_start": "YYYY-MM-DD"|null, "charge_end": "YYYY-MM-DD"|null}], "total": number|null}.
billed_cost exactly as printed (negative for credits). Include tax lines as rows with service_raw "Tax".
Do not include subtotal or total lines as rows. Put the invoice grand total in "total".
A table with one column per period (2026-01, Feb 2026, Q1...): one row per line and period, the period in
charge_start and charge_end (its first and last day), never in description; a "Total" column is not a period.
Return ONLY JSON."""


def extract_rows(llm: LLM, preview: dict) -> dict:
    return _ask(llm, EXTRACT_GUIDE, render_preview(preview), rows_json_schema(), max_tokens=8000)

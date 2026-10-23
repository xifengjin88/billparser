"""Optional LLM fallback for strings the deterministic steps can't resolve.

The model may only answer with one of the candidate keys or NONE, so it can't
invent a service. Results are cached by the resolver, so each string costs one
call ever. Set ANTHROPIC_API_KEY to enable; SVCRES_MODEL overrides the model.
"""

from __future__ import annotations

import os

import requests

from .catalog import Catalog

PROMPT = """You map cloud billing line labels to canonical cloud services.

Label: {raw}
Provider hint: {provider}

Candidates (key | service | category):
{cands}

Reply with exactly one candidate key from the list, or NONE if none clearly fits.
No other text."""


def make_llm_fallback(catalog: Catalog):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    model = os.environ.get("SVCRES_MODEL", "claude-haiku-5-5")

    def fallback(raw: str, provider: str | None, cands: list[tuple[str, float]]) -> str | None:
        lines = []
        for key, _ in cands:
            s = catalog.services[key]
            lines.append(f"{key} | {s['name']} | {s['category']}")
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": model, "max_tokens": 50,
                      "messages": [{"role": "user", "content": PROMPT.format(
                          raw=raw, provider=provider or "unknown", cands="\n".join(lines))}]},
                timeout=30,
            )
            r.raise_for_status()
            answer = "".join(b.get("text", "") for b in r.json()["content"]).strip()
        except requests.RequestException as e:
            print(f"  LLM fallback failed for {raw!r}: {e}")
            return None
        valid = {k for k, _ in cands}
        return answer if answer in valid else None

    return fallback

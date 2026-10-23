"""Layout fingerprints: exact hash for instant reuse, token sets for similarity.

Used twice: to route a file to a stored spec, and to retrieve the most similar
verified specs as examples for the LLM when a new layout appears.
"""

from __future__ import annotations

import hashlib
import re

from .docmodel import Document, is_numberish
from .layout import find_header, norm_h

_WORD = re.compile(r"[a-z][a-z]{3,}")


def table_headers(doc: Document, max_regions: int = 4) -> list[str]:
    heads: list[str] = []
    for region in doc.regions[:max_regions]:
        if region.is_frame:
            heads += [str(c) for c in region.df.columns]
        elif region.rows:
            i = find_header(region)
            if i is not None:
                heads += [c.text for c in region.rows[i].cells if c.text]
    return heads


def top_words(doc: Document, n_rows: int = 8) -> set[str]:
    for region in doc.regions:
        if not region.is_frame and region.rows:
            words = set()
            for row in region.rows[:n_rows]:
                for c in row.cells:
                    if not is_numberish(c.text):
                        words |= set(_WORD.findall(c.text.lower()))
            return words
    return set()


def fingerprint(doc: Document) -> dict:
    headers = sorted({norm_h(h) for h in table_headers(doc) if norm_h(h) and not norm_h(h).startswith("col ")})
    words = sorted(top_words(doc))
    kinds = sorted({r.kind for r in doc.regions})
    raw = "|".join([doc.file_type, ",".join(kinds), ";".join(headers)])
    return {"hash": hashlib.sha1(raw.encode()).hexdigest()[:16], "file_type": doc.file_type,
            "headers": headers, "words": words}


def similarity(a: dict, b: dict) -> float:
    if a["file_type"] != b["file_type"] and {a["file_type"], b["file_type"]} - {"csv", "xlsx", "json", "parquet"}:
        return 0.0                                              # PDF vs tabular never match
    ha, hb = set(a["headers"]), set(b["headers"])
    wa, wb = set(a.get("words", [])), set(b.get("words", []))
    hj = len(ha & hb) / len(ha | hb) if ha | hb else 0.0
    wj = len(wa & wb) / len(wa | wb) if wa | wb else 0.0
    if not ha and not hb:
        return wj
    return 0.75 * hj + 0.25 * wj if (wa or wb) else hj

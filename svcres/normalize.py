"""Text normalization shared by the catalog builder and the resolver.

Both sides must normalize the same way, or exact-name lookups will miss.
"""

from __future__ import annotations

import re
import unicodedata

from .seeds import AZURE_ABBREVIATIONS

_NOISE_WORDS = {"service", "services", "the"}
_PREFIXES = ("amazon ", "aws ", "microsoft ", "azure ", "google cloud ", "google ", "gcp ")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm(text: object) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    s = _NON_ALNUM.sub(" ", s.lower()).strip()
    return re.sub(r"\s+", " ", s)


def norm_loose(text: object) -> str:
    """norm() plus vendor prefixes and filler words removed.

    "Amazon Elastic Compute Cloud" and "Elastic Compute Cloud" both become
    "elastic compute cloud". Used as a second exact-match key and for fuzzy search.
    """
    s = norm(text)
    for p in _PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
            break
    words = [w for w in s.split() if w not in _NOISE_WORDS]
    return " ".join(words) or s


def code_key(text: object) -> str:
    """Product codes compare case-insensitively with surrounding whitespace removed."""
    return str(text).strip().upper() if text is not None else ""


def azure_head(text: object) -> str:
    """The service part of a Microsoft invoice product name, its short words written out:
    "Az DDOS Prot-NW Prot Plan-1 Hr" -> "azure ddos protection", "VM Dadsv5 Series" -> "virtual machines dadsv5
    series". Empty when the text has no letters."""
    head = str(text or "").split("-", 1)[0].split(":", 1)[0]

    def word(w: str) -> str:                            # "p/l" is one short word; "sgl/el" is two
        w = w.lower()
        if w in AZURE_ABBREVIATIONS or "/" not in w:
            return AZURE_ABBREVIATIONS.get(w, w)
        return " ".join(AZURE_ABBREVIATIONS.get(x, x) for x in w.split("/"))
    joined = " ".join(word(w) for w in head.split())
    return norm(AZURE_ABBREVIATIONS.get(joined, joined))

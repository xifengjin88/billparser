"""Units, spelled one way, and the factor that turns one input unit into the normalized one: a price per hour
matches usage counted in seconds, a price per 1M requests usage counted in requests."""

from __future__ import annotations

import re

from billparse.transforms import parse_block_unit

# normalized unit -> spellings (lower case, spaces collapsed), and the factor of one such unit
UNITS: dict[str, dict[str, float]] = {
    "hour": {"hrs": 1, "hr": 1, "hour": 1, "hours": 1, "h": 1, "1 hour": 1, "instance hour": 1, "instance-hours": 1,
             "seconds": 1 / 3600, "second": 1 / 3600, "s": 1 / 3600, "sec": 1 / 3600, "minutes": 1 / 60, "minute": 1 / 60,
             "min": 1 / 60, "days": 24, "day": 24},
    "gb_month": {"gb-mo": 1, "gb-month": 1, "gb/month": 1, "gb month": 1, "gibibyte month": 1, "gib-month": 1,
                 "gb-mo.": 1, "tb-mo": 1024, "tb-month": 1024, "tb/month": 1024},
    "gb": {"gb": 1, "gib": 1, "gigabyte": 1, "gibibyte": 1, "tb": 1024, "mb": 1 / 1024,
           "tebibyte": 1024, "mebibyte": 1 / 1024},          # GCP's unit words (an AWS price list never spells them)
    "request": {"requests": 1, "request": 1, "req": 1, "api calls": 1, "calls": 1, "count": 1, "invocations": 1},
}
SCALE = {"k": 1e3, "m": 1e6, "b": 1e9}
_SPELLING = {s: (unit, f) for unit, forms in UNITS.items() for s, f in forms.items()}
_SCALED = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmb])\s+(.+)$")        # "1M requests", "10K requests"
_BARE_SCALE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmb])$")            # "10K": a block of requests


def normalize(unit: object) -> tuple[str, float] | None:
    """(normalized unit, factor): one input unit is factor normalized units. "Hrs" -> ("hour", 1), "seconds" ->
    ("hour", 1/3600), "1M requests" -> ("request", 1e6), Azure "10 Hours" -> ("hour", 10). Unknown: None."""
    if unit is None:
        return None
    s = re.sub(r"\s+", " ", str(unit).strip().lower())
    if not s:
        return None
    if s in _SPELLING:
        return _SPELLING[s]
    if (m := _BARE_SCALE.match(s)):
        return "request", float(m.group(1)) * SCALE[m.group(2)]
    if (m := _SCALED.match(s)) and (inner := normalize(m.group(3))):
        return inner[0], inner[1] * float(m.group(1)) * SCALE[m.group(2)]
    block = parse_block_unit(s)
    if block and block[1].lower() != s and (inner := normalize(block[1])):
        return inner[0], inner[1] * block[0]
    return None

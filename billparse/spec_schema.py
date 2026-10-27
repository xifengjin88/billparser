"""JSON Schemas of what the LLM writes: a spec (llm.generate_spec) and directly extracted rows (llm.extract_rows).

Built from the constants check_spec uses (spec.ALLOWED_TOP, transforms.CANONICAL, transforms.TRANSFORMS), and as loose
as check_spec everywhere else: the nested options (table, detect, reshape, filters, fields, totals, tree, context,
meta) are objects the interpreter and check_spec judge, so the schema never rejects a spec check_spec accepts. The LLM
gets the schema as its answer tool's input schema (llm.AnthropicLLM.complete_json), so the answer is a whole JSON
object: no cut-off text, no stray prose. A schema-valid spec can still fail check_spec (an expression, a regex): that
stays a repair round.
"""

from __future__ import annotations

from .spec import ALLOWED_TOP
from .transforms import CANONICAL, TRANSFORMS

LOOSE_OBJECT = {"type": "object"}


def _transform() -> dict:
    """A transform step: its name ("number"), or {name: argument} ({"date": "%m/%d/%Y"})."""
    names = sorted(TRANSFORMS)
    return {"anyOf": [{"type": "string", "enum": names},
                      {"type": "object", "minProperties": 1, "maxProperties": 1, "propertyNames": {"enum": names}}]}


def spec_json_schema() -> dict:
    fields = sorted(CANONICAL)
    column = {"type": "object",
              "properties": {"from": {"type": "array", "items": {"type": "string"}},
                             "t": {"type": ["array", "null"], "items": _transform()},
                             "coalesce": {"type": "boolean"},
                             "const": {}},
              "additionalProperties": True}
    props: dict = {k: {} for k in sorted(ALLOWED_TOP)}            # any value: check_spec and the interpreter judge
    props.update({
        "source": {"type": "string"},
        "detect": LOOSE_OBJECT, "meta": LOOSE_OBJECT, "table": LOOSE_OBJECT, "reshape": LOOSE_OBJECT,
        "fields": LOOSE_OBJECT, "totals": LOOSE_OBJECT, "tree": LOOSE_OBJECT, "context": LOOSE_OBJECT,
        "filters": {"type": "array", "items": LOOSE_OBJECT},
        # canonical fields, and "_name" internal fields a step reads
        "columns": {"type": "object", "propertyNames": {"anyOf": [{"enum": fields}, {"pattern": "^_"}]},
                    "additionalProperties": column},
        "derive": {"type": "object", "propertyNames": {"enum": fields}, "additionalProperties": {"type": "string"}},
        "lines": {"type": "array", "items": {"type": "object", "properties": {"pattern": {"type": "string"}},
                                             "required": ["pattern"]}},
        "maps": {"type": "object", "additionalProperties": {"type": "string"}},
    })
    return {"type": "object", "properties": props, "additionalProperties": False}


def rows_json_schema() -> dict:
    """llm.EXTRACT_GUIDE's answer: {"rows": [...], "total": number|null}."""
    text, number = {"type": ["string", "null"]}, {"type": ["number", "null"]}
    row = {"type": "object",
           "properties": {"service_raw": text, "description": text, "billed_cost": {"type": "number"},
                          "quantity": number, "unit": text, "unit_price": number, "charge_start": text,
                          "charge_end": text},
           "required": ["billed_cost"], "additionalProperties": True}
    return {"type": "object", "properties": {"rows": {"type": "array", "items": row}, "total": number},
            "required": ["rows"], "additionalProperties": False}

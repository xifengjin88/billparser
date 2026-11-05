"""Terraform as an inventory (iac.py gives the rows): what the files launch, how many, of what size. No prices.

  Values      variables at their defaults, then terraform.tfvars and *.auto.tfvars (and one other .tfvars file, as
              -var-file), then a module's inputs; locals; each.key / each.value; `cond ? a : b`, comparisons,
              && || !, + - * /, and the common functions (length, lookup, merge, concat, keys, values, max, min,
              element, try, coalesce, to*). A value the files do not give (a variable with no default, a data source,
              an attribute known only after apply) is Unknown: never a guess. The row keeps it empty and a gap names
              the expression and why.
  Counting    `count`, `for_each` (a row for each key when the keys differ in size), the count of a module; an Auto
              Scaling group runs desired_capacity instances of its launch template; an EKS node group its
              scaling_config.desired_size; an ECS service its desired_count tasks of its task definition's size.
  Modules     a local module (source "./db") whose files are in the upload is read with its inputs; a remote
              module (registry, git) is a gap: its resources are not in the upload.
  Storage     root_block_device / ebs_block_device of an instance and block_device_mappings of a launch template are
              volume rows (the instance's count); OpenSearch and MSK volumes likewise.
  Usage       what a resource is priced by and an inventory cannot give (GB stored, requests, LCU, ...) is a gap
              (iac.USAGE). A resource type billparse does not know, and that is not free, is a row and a gap.
"""

from __future__ import annotations

import math
import posixpath
import re
from dataclasses import dataclass, field

from .iac import FREE, NODES, RTYPE_SIZE, SERVICES, SIZE, Item, Result, _gap, _number

BLOCK = re.compile(r"^[ \t]*(resource|variable|provider|data|locals|module)(?:[ \t]+\"([\w-]+)\")?"
                   r"(?:[ \t]+\"([\w-]+)\")?[ \t]*\{", re.M)
ENTRY = re.compile(r"\s*,?\s*(\"[^\"]*\"|[\w-]+)((?:[ \t]+\"[^\"]*\")*)\s*(=(?!=)|:|\{)")
REF = re.compile(r"(var|local|each|count)\.([\w-]+)((?:\.[\w-]+|\[\"?[\w-]+\"?\])*)")
LATER = re.compile(r"(data|module|aws_\w+)\.[\w-]+(\.|\[|$)")     # a data source, a module output, an attribute
MODULE_META = {"source", "version", "count", "for_each", "providers", "depends_on"}


@dataclass(frozen=True)
class Unknown:
    """A Terraform value the files do not give: the expression and why (a variable with no default, ...)."""
    expr: str
    why: str


# ---------------------------------------------------------------- reading HCL
def strip_comments(text: str) -> str:
    """Drop # and // comments and /* */ blocks, keeping the line breaks (so line numbers stay right)."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    out = []
    for line in text.split("\n"):
        cut, quote = len(line), False
        for i, ch in enumerate(line):
            if ch == '"' and (i == 0 or line[i - 1] != "\\"):
                quote = not quote
            elif not quote and (ch == "#" or line.startswith("//", i)):
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def _scan(text: str, i: int, stop: str) -> int:
    """The index of the first character of `stop` at bracket depth 0 and outside a string, from i (else the end).
    "}" as stop: the brace that closes the one before i."""
    depth, quote = 0, False
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = False
        elif ch == '"':
            quote = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0 and ch in stop:
                return i
            depth -= 1
        elif depth == 0 and ch in stop:
            return i
        i += 1
    return len(text)


def _entries(body: str, objects: bool = False) -> tuple[dict[str, str], list[tuple[str, str, int, str]]]:
    """A block body (or an object, objects=True: commas also end a value): its own `key = value` attributes (a value
    can span lines) and its nested blocks as (name, labels, offset in body, body)."""
    attrs: dict[str, str] = {}
    blocks: list[tuple[str, str, int, str]] = []
    i = 0
    while i < len(body):
        m = ENTRY.match(body, i)
        if not m:
            nl = body.find("\n", i)
            i = len(body) if nl < 0 else nl + 1
            continue
        key = m.group(1).strip('"')
        if m.group(3) == "{" and not objects:
            close = _scan(body, m.end(), "}")
            blocks.append((key, m.group(2).strip(), m.start(1), body[m.end():close]))
            i = close + 1
        else:
            end = _scan(body, m.end(), ",\n" if objects else "\n")
            attrs[key] = body[m.end():end].strip()
            i = end + 1
    return attrs, blocks


@dataclass
class Node:
    """A block: its attributes (expressions) and its nested blocks."""
    name: str
    line: int
    attrs: dict
    children: list[Node] = field(default_factory=list)

    def child(self, name: str) -> Node | None:
        return next((c for c in self.children if c.name == name), None)

    def all(self, name: str) -> list[Node]:
        return [c for c in self.children if c.name == name]


def _node(name: str, body: str, base: int, line_of) -> Node:
    attrs, blocks = _entries(body)
    return Node(name, line_of(base), attrs, [_node(n, b, base + off, line_of) for n, _, off, b in blocks])


def _items(inner: str) -> list[str]:
    out, i = [], 0
    while i < len(inner):
        end = _scan(inner, i, ",\n")
        if inner[i:end].strip():
            out.append(inner[i:end].strip())
        i = end + 1
    return out


# ---------------------------------------------------------------- values
@dataclass
class Scope:
    variables: dict = field(default_factory=dict)      # name -> value, or Unknown (no default)
    locals: dict = field(default_factory=dict)         # name -> expression
    each: tuple | None = None                          # (each.key, each.value) inside a for_each
    done: dict = field(default_factory=dict)

    def local(self, name: str):
        if name not in self.done:
            if name not in self.locals:
                return Unknown(f"local.{name}", f"local.{name} is not in the files")
            self.done[name] = Unknown(f"local.{name}", f"local.{name} refers to itself")
            self.done[name] = value(self.locals[name], Scope(self.variables, self.locals, None, self.done))
        return self.done[name]

    def with_each(self, key, val) -> Scope:
        return Scope(self.variables, self.locals, (key, val), self.done)


def _ref(s: str, scope: Scope):
    m = REF.fullmatch(s)
    root, name, path = m.group(1), m.group(2), m.group(3)
    if root == "var":
        base = scope.variables.get(name, Unknown(s, f"var.{name} is not declared in the files"))
    elif root == "local":
        base = scope.local(name)
    elif root == "count":
        return Unknown(s, f"{s} differs for each instance")
    elif scope.each is None:
        return Unknown(s, f"{s} outside a for_each")
    else:
        base = scope.each[0] if name == "key" else scope.each[1] if name == "value" else Unknown(s, f"{s} is unknown")
    for part in re.findall(r"[\w-]+", path):
        if isinstance(base, Unknown):
            break
        if isinstance(base, dict) and part in base:
            base = base[part]
        elif isinstance(base, list) and part.isdigit() and int(part) < len(base):
            base = base[int(part)]
        else:
            base = Unknown(s, f"{s}: no {part}")
    return Unknown(s, base.why) if isinstance(base, Unknown) else base


def _unknown_in(values: list) -> Unknown | None:
    return next((v for v in values if isinstance(v, Unknown)), None)


def _call(fn: str, args: list[str], scope: Scope, s: str):
    if fn == "try":
        for a in args:
            v = value(a, scope)
            if not isinstance(v, Unknown):
                return v
        return Unknown(s, "no argument of try() can be read")
    vals = [value(a, scope) for a in args]
    if fn == "coalesce":
        for v in vals:
            if isinstance(v, Unknown):
                return Unknown(s, v.why)
            if v not in (None, ""):
                return v
        return Unknown(s, "every argument of coalesce() is empty")
    if (u := _unknown_in(vals)) is not None:
        return Unknown(s, u.why)
    try:
        if fn == "length":
            return len(vals[0])
        if fn == "lookup":
            return vals[0][vals[1]] if vals[1] in vals[0] else vals[2] if len(vals) > 2 else \
                Unknown(s, f"lookup(): no key {vals[1]}")
        if fn == "merge":
            out: dict = {}
            for v in vals:
                out.update(v)
            return out
        if fn == "concat":
            return [x for v in vals for x in v]
        if fn == "keys":
            return sorted(vals[0])
        if fn == "values":
            return [vals[0][k] for k in sorted(vals[0])]
        if fn in ("max", "min"):
            nums = [_number(v) for v in vals]
            return (max if fn == "max" else min)(nums) if None not in nums else Unknown(s, f"{fn}(): not numbers")
        if fn == "element":
            return vals[0][int(vals[1]) % len(vals[0])]
        if fn in ("tonumber", "ceil", "floor", "abs"):
            n = _number(vals[0])
            if n is None:
                return Unknown(s, f"{fn}(): not a number")
            return n if fn == "tonumber" else math.ceil(n) if fn == "ceil" else math.floor(n) if fn == "floor" \
                else abs(n)
        if fn == "tostring":
            return str(vals[0])
        if fn in ("tolist", "tomap"):
            return vals[0]
        if fn == "toset":
            return list(dict.fromkeys(vals[0]))
        if fn in ("upper", "lower"):
            return str(vals[0]).upper() if fn == "upper" else str(vals[0]).lower()
        if fn == "contains":
            return vals[1] in vals[0]
    except (TypeError, KeyError, IndexError, ValueError, ZeroDivisionError):
        return Unknown(s, f"{fn}() cannot be worked out from the files")
    return Unknown(s, f"{fn}() is not read by billparse")


def _split_op(s: str, ops: tuple[str, ...]) -> tuple[int, str] | None:
    """The last binary operator of ops (written with spaces around it) at depth 0, outside strings: (index, op).
    The last one: the operators of one level group from the left."""
    best = None
    for op in ops:
        i = 0
        while (k := s.find(op, i)) >= 0:
            if _depth0(s, k) and (best is None or k > best[0]):
                best = (k, op)
            i = k + 1
    return best


def _depth0(s: str, k: int) -> bool:
    depth, quote, i = 0, False, 0
    while i < k:
        ch = s[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = False
        elif ch == '"':
            quote = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        i += 1
    return depth == 0 and not quote


def value(expr, scope: Scope):
    """A Terraform expression's value (a string, number, bool, list or object), or Unknown."""
    s = str(expr).strip()
    if not s:
        return Unknown(s, "no value")
    while s.startswith("(") and _scan(s, 1, ")") == len(s) - 1:
        s = s[1:-1].strip()
    q = _scan(s, 0, "?")
    if q < len(s):
        c = q + 1 + _scan(s[q + 1:], 0, ":")
        cond = value(s[:q], scope)
        if isinstance(cond, bool):
            return value(s[q + 1:c] if cond else s[c + 1:], scope)
        return Unknown(s, cond.why if isinstance(cond, Unknown) else f"{s[:q].strip()} is not true or false")
    for ops in ((" || ",), (" && ",), (" == ", " != "), (" >= ", " <= ", " > ", " < "), (" + ", " - "),
                (" * ", " / ", " % ")):
        if (hit := _split_op(s, ops)) is not None:
            k, op = hit
            a, b = value(s[:k], scope), value(s[k + len(op):], scope)
            return _binary(op.strip(), a, b, s)
    if s.startswith("!"):
        v = value(s[1:], scope)
        return not v if isinstance(v, bool) else Unknown(s, v.why if isinstance(v, Unknown) else "not true or false")
    if (m := re.fullmatch(r"([a-z_]\w*)\(", s[:s.find("(") + 1])) and s.endswith(")") and \
            _scan(s, len(m.group(0)), ")") == len(s) - 1:
        return _call(m.group(1), _items(s[len(m.group(0)):-1]), scope, s)
    if s in ("true", "false"):
        return s == "true"
    if s == "null":
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return float(s) if "." in s else int(s)
    if (m := re.fullmatch(r'"((?:[^"\\]|\\.)*)"', s)):
        text = m.group(1)
        for m in reversed(list(re.finditer(r"\$\{([^}]*)\}", text))):
            v = value(m.group(1), scope)
            if isinstance(v, (Unknown, list, dict)):
                return Unknown(s, v.why if isinstance(v, Unknown) else f"{s} is not one value")
            text = text[:m.start()] + (str(v).lower() if isinstance(v, bool) else str(v)) + text[m.end():]
        return text
    if s.startswith("[") and s.endswith("]"):
        return [value(x, scope) for x in _items(s[1:-1])]
    if s.startswith("{") and s.endswith("}"):
        attrs, _ = _entries(s[1:-1], objects=True)
        return {k: value(v, scope) for k, v in attrs.items()}
    if REF.fullmatch(s):
        return _ref(s, scope)
    if LATER.match(s):
        return Unknown(s, f"{s} is known only when Terraform runs (a data source, a module output or an attribute)")
    return Unknown(s, f"{s} cannot be read from the files")


def _binary(op: str, a, b, s: str):
    if (u := _unknown_in([a, b])) is not None:
        return Unknown(s, u.why)
    if op in ("||", "&&"):
        if not (isinstance(a, bool) and isinstance(b, bool)):
            return Unknown(s, "not true or false")
        return (a or b) if op == "||" else (a and b)
    if op in ("==", "!="):
        return (str(a) == str(b)) == (op == "==")
    x, y = _number(a), _number(b)
    if x is None or y is None:
        return Unknown(s, f"{s}: not numbers")
    if op in (">=", "<=", ">", "<"):
        return {">=": x >= y, "<=": x <= y, ">": x > y, "<": x < y}[op]
    if op in ("/", "%") and y == 0:
        return Unknown(s, f"{s}: division by zero")
    return _number({"+": x + y, "-": x - y, "*": x * y, "/": x / y, "%": x % y}[op])


# ---------------------------------------------------------------- the files of a module
@dataclass
class Module:
    """The files of one directory: variables and locals are shared by all its files."""
    dir: str
    variables: dict = field(default_factory=dict)     # name -> default expression, or Unknown (no default)
    locals: dict = field(default_factory=dict)
    region: tuple | None = None                       # (expression, src) of the aws provider's region
    resources: list = field(default_factory=list)     # (path, rtype, name, Node)
    modules: list = field(default_factory=list)       # (path, name, Node)
    tfvars: list = field(default_factory=list)        # (file name, {name: expression})


def parse_file(path: str, lines: list[tuple[int, str]], mod: Module) -> None:
    numbers = [n for n, _ in lines]
    text = strip_comments("\n".join(line for _, line in lines))

    def line_of(offset: int) -> int:
        return numbers[min(text.count("\n", 0, offset), len(numbers) - 1)] if numbers else 0

    if path.endswith(".tfvars"):
        mod.tfvars.append((posixpath.basename(path), _entries(text)[0]))
        return
    for m in BLOCK.finditer(text):
        kind, a, b = m.group(1), m.group(2), m.group(3)
        close = _scan(text, m.end(), "}")
        node = _node(kind, text[m.end():close], m.end(), line_of)
        node.line = line_of(m.start(1))
        if kind == "variable" and a:
            mod.variables[a] = node.attrs["default"] if "default" in node.attrs else \
                Unknown(f"var.{a}", f"var.{a} has no default")
        elif kind == "locals":
            mod.locals.update(node.attrs)
        elif kind == "provider" and a == "aws" and "region" in node.attrs and mod.region is None:
            mod.region = (node.attrs["region"], f"{path}:{node.line}")
        elif kind == "resource" and a and b:
            mod.resources.append((path, a, b, node))
        elif kind == "module" and a:
            mod.modules.append((path, a, node))


def _var_files(mod: Module, res: Result) -> list[dict]:
    """The .tfvars files Terraform reads by itself (terraform.tfvars, then *.auto.tfvars), then one other as
    -var-file; several others: which one is meant is not known (a gap), none is used."""
    auto = [v for n, v in mod.tfvars if n == "terraform.tfvars"] + \
        [v for n, v in sorted(mod.tfvars) if n.endswith(".auto.tfvars")]
    other = [(n, v) for n, v in mod.tfvars if n != "terraform.tfvars" and not n.endswith(".auto.tfvars")]
    if len(other) > 1:
        _gap(res, "tfvars", f"{mod.dir or '.'}", "variables",
             f"several variable files ({', '.join(n for n, _ in other)}): which one is meant is not known; "
             "none of them is used")
        other = []
    return auto + [v for _, v in other]


# ---------------------------------------------------------------- the inventory
@dataclass
class Ctx:
    res: Result
    scope: Scope
    templates: dict            # launch template name -> Node
    refs: dict                 # "rtype.name" -> Node: what a resource refers to (a task definition, a cluster)
    prefix: str                # "module.db." inside a module
    mult: int | None           # the module's count (None: not known)
    mods: dict = field(default_factory=dict)   # each module around it: its name -> its count (None: not known)


def _set(ctx: Ctx, name: str, src: str, size: dict, fld: str, key: str, expr: str) -> None:
    v = value(expr, ctx.scope)
    if isinstance(v, Unknown):
        _gap(ctx.res, name, src, fld, f"{key} = {expr}: {v.why}")
    elif v is not None:
        size[fld] = v


def _count(ctx: Ctx, expr: str, v, name: str, src: str) -> int | None:
    if isinstance(v, Unknown):
        _gap(ctx.res, name, src, "quantity", f"{expr}: {v.why}; the number is not known")
        return None
    n = _number(v)
    if n is None:
        _gap(ctx.res, name, src, "quantity", f"{expr}: not a number")
    return None if n is None else int(n)


def _times(a: int | None, b: int | None) -> int | None:
    return None if a is None or b is None else a * b


def _volume(ctx: Ctx, path: str, node: Node, vname: str, count: int | None, note: str, size_key: str = "volume_size",
            type_key: str = "volume_type", default_note: str | None = None, rtype: str = "aws_ebs_volume") -> Item:
    """A volume of a resource (rtype: an EC2 EBS volume, else the service that bills it): size, type, and the IOPS and
    throughput it provisions."""
    src = f"{path}:{node.line}"
    size: dict = {}
    for key, fld in ((size_key, "storage_gb"), (type_key, "storage_type"), ("iops", "iops"), ("throughput", "throughput")):
        if key in node.attrs:
            _set(ctx, vname, src, size, fld, key, node.attrs[key])
    if size_key not in node.attrs and default_note:
        _gap(ctx.res, vname, src, "storage_gb", default_note)
    return Item(path, node.line, vname, rtype, count, size, note)


def _instance(ctx: Ctx, path: str, rtype: str, node: Node, name: str, count: int | None) -> list[Item]:
    """The rows of one resource instance (or of all of them, when they are alike): the resource and its volumes."""
    src = f"{path}:{node.line}"
    rmap = RTYPE_SIZE.get(rtype, {})
    size: dict = {}
    for k, expr in node.attrs.items():
        if (fld := SIZE.get(k) or rmap.get(k)):
            _set(ctx, name, src, size, fld, k, expr)
    for k, expr in node.attrs.items():
        if k in NODES and count is not None:
            count = _count(ctx, f"{k} = {expr}", value(expr, ctx.scope), name, src)
    note, extra = "", []
    if rtype == "aws_elasticache_replication_group" and "num_node_groups" in node.attrs:
        groups = _count(ctx, "num_node_groups", value(node.attrs["num_node_groups"], ctx.scope), name, src)
        replicas = _count(ctx, "replicas_per_node_group",
                          value(node.attrs.get("replicas_per_node_group", "0"), ctx.scope), name, src)
        count = _times(groups, None if replicas is None else replicas + 1)
        note = f"{node.attrs['num_node_groups']} shards x (1 + {node.attrs.get('replicas_per_node_group', 0)} replicas)"
    elif rtype == "aws_autoscaling_group":
        key = next((k for k in ("desired_capacity", "min_size") if k in node.attrs), None)
        count = _count(ctx, f"{key} = {node.attrs[key]}", value(node.attrs[key], ctx.scope), name, src) if key \
            else None
        if key is None:
            _gap(ctx.res, name, src, "quantity", "no desired_capacity or min_size: the number of instances is not known")
        note = f"desired {count} (min {node.attrs.get('min_size')}, max {node.attrs.get('max_size')})"
        lt = node.child("launch_template")
        ref = re.match(r"aws_launch_template\.([\w-]+)\.", (lt.attrs.get("id") or lt.attrs.get("name") or "")) \
            if lt else None
        tpl = ctx.templates.get(ref.group(1)) if ref else None
        if node.child("mixed_instances_policy"):
            _gap(ctx.res, name, src, "instance_type", "mixed_instances_policy: several instance types, the share "
                 "of each is not in the file")
        elif tpl is None:
            _gap(ctx.res, name, src, "instance_type", "no launch template in the files gives the instance type")
        else:
            if "instance_type" in tpl.attrs:
                _set(ctx, name, src, size, "instance_type", "instance_type", tpl.attrs["instance_type"])
            note += f"; launch template {ref.group(1)}"
            for i, bdm in enumerate(tpl.all("block_device_mappings")):
                ebs = bdm.child("ebs")
                if ebs:
                    dev = value(bdm.attrs.get("device_name", f'"{i}"'), ctx.scope)
                    extra.append(_volume(ctx, path, ebs, f"{name}.block_device_mappings[{dev}]", count,
                                         f"volume of launch template {ref.group(1)}"))
    elif rtype == "aws_eks_node_group":
        sc = node.child("scaling_config")
        count = _count(ctx, "scaling_config.desired_size", value(sc.attrs["desired_size"], ctx.scope), name, src) \
            if sc and "desired_size" in sc.attrs else None
        if count is None and not (sc and "desired_size" in sc.attrs):
            _gap(ctx.res, name, src, "quantity", "no scaling_config.desired_size")
        types = value(node.attrs["instance_types"], ctx.scope) if "instance_types" in node.attrs else None
        if isinstance(types, Unknown):
            _gap(ctx.res, name, src, "instance_type", f"instance_types: {types.why}")
        elif types is None:
            _gap(ctx.res, name, src, "instance_type", "instance_types is not set: AWS uses t3.medium")
        elif len(types) == 1:
            size["instance_type"] = types[0]
        else:
            _gap(ctx.res, name, src, "instance_type", f"instance_types {types}: the share of each is not in the file")
        note = f"scaling_config desired {count}"
        extra.append(Item(path, node.line, f"{name}.disk", "aws_ebs_volume", count,
                          {"storage_gb": _number(value(node.attrs.get("disk_size", "20"), ctx.scope))},
                          "node disk (disk_size, 20 GB when not set)"))
    elif rtype == "aws_ecs_service":
        lt = value(node.attrs.get("launch_type", '"EC2"'), ctx.scope)
        if lt != "FARGATE":
            if node.child("capacity_provider_strategy"):
                _gap(ctx.res, name, src, "launch_type", "capacity_provider_strategy: Fargate or EC2 is not known")
            else:
                ctx.res.excluded.append({"name": name, "type": rtype, "src": src,
                                         "reason": f"launch type {lt}: its tasks run on EC2 instances counted "
                                                   "elsewhere"})
                return []
        count = _count(ctx, "desired_count", value(node.attrs.get("desired_count", "0"), ctx.scope), name, src)
        ref = re.match(r"aws_ecs_task_definition\.([\w-]+)\.", node.attrs.get("task_definition", ""))
        task = ctx.refs.get(f"aws_ecs_task_definition.{ref.group(1)}") if ref else None
        if task is None:
            _gap(ctx.res, name, src, "vcpu", "the task definition is not in the files: the task size is not known")
        else:
            for key, fld in (("cpu", "vcpu"), ("memory", "memory_gb")):
                v = value(task.attrs.get(key, ""), ctx.scope) if key in task.attrs else None
                n = _number(v) if not isinstance(v, Unknown) else None
                if n is None:
                    _gap(ctx.res, name, src, fld, f"task definition {ref.group(1)}: {key} is not known")
                else:
                    size[fld] = n / 1024
            rp = task.child("runtime_platform")
            arch = value(rp.attrs.get("cpu_architecture", '"X86_64"'), ctx.scope) if rp else "X86_64"
            size["architecture"] = arch if isinstance(arch, str) else "X86_64"
        note = f"desired_count {count}, Fargate"
    elif rtype in ("aws_opensearch_domain", "aws_elasticsearch_domain"):
        cc = node.child("cluster_config")
        cattrs = cc.attrs if cc else {}
        if "instance_type" in cattrs:
            _set(ctx, name, src, size, "instance_type", "instance_type", cattrs["instance_type"])
        else:
            _gap(ctx.res, name, src, "instance_type", "cluster_config.instance_type is not set: AWS uses "
                 "m4.large.search (Elasticsearch) or r6g.large.search (OpenSearch)")
        count = _count(ctx, "instance_count", value(cattrs.get("instance_count", "1"), ctx.scope), name, src)
        if value(cattrs.get("dedicated_master_enabled", "false"), ctx.scope) is True:
            m_size: dict = {}
            if "dedicated_master_type" in cattrs:
                _set(ctx, f"{name}.master", src, m_size, "instance_type", "dedicated_master_type",
                     cattrs["dedicated_master_type"])
            m_count = _count(ctx, "dedicated_master_count", value(cattrs.get("dedicated_master_count", "3"),
                                                                  ctx.scope), f"{name}.master", src)
            extra.append(Item(path, node.line, f"{name}.master", rtype, m_count, m_size, "dedicated masters",
                              factors={f"{name}.master": m_count}))
        ebs = node.child("ebs_options")
        if ebs and value(ebs.attrs.get("ebs_enabled", "true"), ctx.scope) is not False:
            extra.append(_volume(ctx, path, ebs, f"{name}.ebs_options", count, "volume of each data node",
                                 rtype="opensearch_volume"))
    elif rtype == "aws_rds_cluster_instance":
        ref = re.match(r"aws_rds_cluster\.([\w-]+)\.", node.attrs.get("cluster_identifier", ""))
        cluster = ctx.refs.get(f"aws_rds_cluster.{ref.group(1)}") if ref else None
        if cluster is not None and "storage_type" in cluster.attrs:
            _set(ctx, name, src, size, "storage_type", "storage_type", cluster.attrs["storage_type"])
    elif rtype == "aws_redshift_cluster":
        count = _count(ctx, "number_of_nodes", value(node.attrs.get("number_of_nodes", "1"), ctx.scope), name, src)
    elif rtype == "aws_msk_cluster":
        count = _count(ctx, "number_of_broker_nodes", value(node.attrs.get("number_of_broker_nodes", "1"),
                                                            ctx.scope), name, src)
        info = node.child("broker_node_group_info")
        if info and "instance_type" in info.attrs:
            _set(ctx, name, src, size, "instance_type", "instance_type", info.attrs["instance_type"])
        si = info.child("storage_info") if info else None
        ebs = si.child("ebs_storage_info") if si else None
        if ebs:
            extra.append(_volume(ctx, path, ebs, f"{name}.ebs_storage_info", count, "volume of each broker",
                                 rtype="msk_volume"))
    elif rtype == "aws_kinesis_stream" and "shard_count" in node.attrs:
        count = _count(ctx, "shard_count", value(node.attrs["shard_count"], ctx.scope), name, src)
    elif rtype == "aws_vpc_endpoint":
        kind = value(node.attrs.get("vpc_endpoint_type", '"Gateway"'), ctx.scope)
        if kind == "Gateway":
            ctx.res.excluded.append({"name": name, "type": rtype, "src": src, "reason": "a gateway endpoint: no charge"})
            return []
    elif rtype == "aws_instance":
        for bname, default in (("root_block_device", "volume_size is not set: the size comes from the AMI"),
                               ("ebs_block_device", None)):
            for i, b in enumerate(node.all(bname)):
                dev = value(b.attrs.get("device_name", f'"{i}"'), ctx.scope)
                vname = f"{name}.{bname}" if bname == "root_block_device" else f"{name}.{bname}[{dev}]"
                extra.append(_volume(ctx, path, b, vname, count, f"{bname} of {name}", default_note=default))
    for d in node.children:
        if d.name == "dynamic":
            _gap(ctx.res, name, src, "dynamic", "a dynamic block: its contents are not read")
    if rtype not in SERVICES:
        _gap(ctx.res, name, src, "service", f"{rtype} is not a resource type billparse knows: it may cost money")
    for it in extra:                                # a volume, a node disk: as many as its resource
        it.factors = it.factors or {name: count}
    return [Item(path, node.line, name, rtype, count, size, note, factors={name: count}), *extra]


def _resource(ctx: Ctx, path: str, rtype: str, rname: str, node: Node) -> None:
    res, scope = ctx.res, ctx.scope
    name, src = f"{ctx.prefix}{rtype}.{rname}", f"{path}:{node.line}"
    if FREE.match(rtype):
        res.excluded.append({"name": name, "type": rtype, "src": src, "reason": "no charge by itself"})
        return

    def add(items: list[Item], note: str | None = None) -> None:
        for it in items:                            # its own count (or its resource's), then each module's
            it.factors = {**ctx.mods, **(it.factors or {it.name: it.count})}
            it.count = _times(it.count, ctx.mult)
            if note and it.name == items[0].name:
                it.note = it.note or note
        res.items.extend(items)

    if "count" in node.attrs:
        expr = f"count = {node.attrs['count']}"
        count = _count(ctx, expr, value(node.attrs["count"], scope), name, src)
        if count == 0:
            res.excluded.append({"name": name, "type": rtype, "src": src,
                                 "reason": f"{expr}: 0 with the variable values"})
            return
        add(_instance(ctx, path, rtype, node, name, count), expr)
        return
    if "for_each" not in node.attrs:
        add(_instance(ctx, path, rtype, node, name, 1))
        return
    keys = value(node.attrs["for_each"], scope)
    if isinstance(keys, Unknown) or not isinstance(keys, (list, dict)):
        _count(ctx, f"for_each = {node.attrs['for_each']}", keys if isinstance(keys, Unknown) else
               Unknown(node.attrs["for_each"], "not a list, set or map"), name, src)
        add(_instance(ctx, path, rtype, node, name, None))
        return
    pairs = list(keys.items()) if isinstance(keys, dict) else [(k, k) for k in dict.fromkeys(keys)]
    if not pairs:
        res.excluded.append({"name": name, "type": rtype, "src": src,
                             "reason": f"for_each = {node.attrs['for_each']}: empty with the variable values"})
        return
    trial = Ctx(Result(), scope, ctx.templates, ctx.refs, ctx.prefix, ctx.mult, ctx.mods)
    each = [_instance(Ctx(trial.res, scope.with_each(k, v), ctx.templates, ctx.refs, ctx.prefix, ctx.mult, ctx.mods),
                      path, rtype, node, name, 1) for k, v in pairs]
    shape = [[(i.attrs, i.count) for i in items] for items in each]
    if all(s == shape[0] for s in shape):           # alike: one row for all keys
        one = Ctx(res, scope.with_each(*pairs[0]), ctx.templates, ctx.refs, ctx.prefix, ctx.mult, ctx.mods)
        items = _instance(one, path, rtype, node, name, len(pairs))
        add(items, f"for_each {[k for k, _ in pairs]}")
        return
    for k, v in pairs:                              # a row for each key: the keys differ
        kname = f'{name}["{k}"]'
        one = Ctx(res, scope.with_each(k, v), ctx.templates, ctx.refs, ctx.prefix, ctx.mult, ctx.mods)
        add(_instance(one, path, rtype, node, kname, 1), f'for_each key "{k}"')


def _module(res: Result, modules: dict[str, Module], d: str, inputs: dict, prefix: str, mult: int | None,
            mods: dict, root: bool, seen: tuple) -> None:
    """mult: the product of the counts of the modules around this one; mods: each of them (name -> count)."""
    mod = modules[d]
    variables = {k: (v if isinstance(v, Unknown) else value(v, Scope())) for k, v in mod.variables.items()}
    if root:
        for tfvars in _var_files(mod, res):
            variables.update({k: value(v, Scope()) for k, v in tfvars.items() if k in variables})
    variables.update({k: v for k, v in inputs.items() if k in variables})
    scope = Scope(variables, mod.locals)
    if mod.region:
        expr, src = mod.region
        v = value(expr, scope)
        if isinstance(v, str):
            res.region = res.region or v
        else:
            _gap(res, f"{prefix}provider.aws", src, "region",
                 f"region = {expr}: {v.why if isinstance(v, Unknown) else 'not a region'}; the region of the "
                 "resources is not known")
    elif root and (mod.resources or mod.modules):
        path = (mod.resources or mod.modules)[0][0]
        _gap(res, "provider.aws", path, "region", "no aws provider region in the files")
    templates = {n: node for _, t, n, node in mod.resources if t == "aws_launch_template"}
    refs = {f"{t}.{n}": node for _, t, n, node in mod.resources if t in ("aws_ecs_task_definition", "aws_rds_cluster")}
    ctx = Ctx(res, scope, templates, refs, prefix, mult, mods)
    for path, rtype, rname, node in mod.resources:
        _resource(ctx, path, rtype, rname, node)
    for path, name, node in mod.modules:
        mname, src = f"{prefix}module.{name}", f"{path}:{node.line}"
        source = value(node.attrs.get("source", ""), scope)
        child = posixpath.normpath(posixpath.join(d, source)) if isinstance(source, str) and \
            source.startswith((".", "/")) else None
        if child == ".":
            child = ""
        if child is None or child not in modules:
            where = "a remote module" if child is None else "its files are not in the upload"
            _gap(res, mname, src, "module", f"source = {node.attrs.get('source')}: {where}; its resources are not "
                 "counted")
            continue
        if child in seen:
            _gap(res, mname, src, "module", "the module calls itself")
            continue
        runs: list[tuple[str, tuple | None, int | None]] = [("", None, 1)]
        if "count" in node.attrs:
            n = _count(ctx, f"count = {node.attrs['count']}", value(node.attrs["count"], scope), mname, src)
            if n == 0:
                res.excluded.append({"name": mname, "type": "module", "src": src,
                                     "reason": f"count = {node.attrs['count']}: 0 with the variable values"})
                continue
            runs = [("", None, n)]
        elif "for_each" in node.attrs:
            keys = value(node.attrs["for_each"], scope)
            if isinstance(keys, (list, dict)):
                pairs = list(keys.items()) if isinstance(keys, dict) else [(k, k) for k in dict.fromkeys(keys)]
                runs = [(f'["{k}"]', (k, v), 1) for k, v in pairs]
            else:
                _count(ctx, f"for_each = {node.attrs['for_each']}", keys if isinstance(keys, Unknown) else
                       Unknown(node.attrs["for_each"], "not a list, set or map"), mname, src)
                runs = [("", None, None)]
        for key, each, n in runs:
            sc = scope.with_each(*each) if each else scope
            ins = {k: value(v, sc) for k, v in node.attrs.items() if k not in MODULE_META}
            _module(res, modules, child, ins, f"{mname}{key}.", _times(mult, n), {**mods, f"{mname}{key}": n}, False,
                    (*seen, child))


def read(files: dict[str, list[tuple[int, str]]], res: Result) -> None:
    """The Terraform files of a document (each a list of (line number, line)): its root modules and the local modules
    they call."""
    mods: dict[str, Module] = {}
    for path, lines in files.items():
        d = posixpath.dirname(path)
        parse_file(path, lines, mods.setdefault(d, Module(d)))
    called = set()
    for d, mod in mods.items():
        for _, _, node in mod.modules:
            src = node.attrs.get("source", "").strip('"')
            if src.startswith((".", "/")):
                c = posixpath.normpath(posixpath.join(d, src))
                called.add("" if c == "." else c)
    for d in sorted(mods):
        if d not in called:
            _module(res, mods, d, {}, "", 1, {}, True, (d,))

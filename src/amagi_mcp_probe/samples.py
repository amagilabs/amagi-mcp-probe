"""Generate a small, valid sample value for a JSON Schema.

The goal is the boring, valid input a careful user would type: required fields only, the lowest
allowed number, the first enum value, a short string that matches the pattern. Every result is
checked against the schema with a real validator before it is used, so a value we return is
always schema-valid. When we can't build one, we raise Unsatisfiable with the reason.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any, Dict, List, Optional

try:  # Python 3.11+
    import re._parser as sre_parse  # type: ignore[import-not-found]
    import re._constants as sre_constants  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Python 3.9/3.10
    import sre_parse  # type: ignore[no-redef]
    import sre_constants  # type: ignore[no-redef]

from .schemas import make_validator

MAX_DEPTH = 24

FORMAT_SAMPLES = {
    "email": "user@example.com",
    "idn-email": "user@example.com",
    "uri": "https://example.com/",
    "url": "https://example.com/",
    "iri": "https://example.com/",
    "uri-reference": "https://example.com/",
    "iri-reference": "https://example.com/",
    "uri-template": "https://example.com/{id}",
    "hostname": "example.com",
    "idn-hostname": "example.com",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "date": "2024-01-01",
    "date-time": "2024-01-01T00:00:00Z",
    "time": "00:00:00Z",
    "duration": "P1D",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "json-pointer": "/sample",
    "relative-json-pointer": "0",
    "regex": "^sample$",
    "byte": "c2FtcGxl",
}

STRING_CANDIDATES = ["sample", "a", "abc", "A", "0", "1", "sample-1", "sample_1", "example.com",
                     "user@example.com", "2024-01-01", "https://example.com/"]


class Unsatisfiable(Exception):
    """No valid sample could be built for this schema."""


def generate(schema: Any, include_optional: bool = False) -> Any:
    """Return a value that validates against ``schema`` or raise Unsatisfiable."""
    gen = _Generator(schema, include_optional)
    value = gen.value(schema, 0)
    errors = _errors(schema, value)
    if errors:
        raise Unsatisfiable(f"generated value fails the schema: {errors[0]}")
    return value


def _errors(schema: Any, value: Any) -> List[str]:
    try:
        validator = make_validator(schema)
        return [e.message for e in validator.iter_errors(value)]
    except Exception as exc:  # unresolvable $ref, invalid schema, ...
        return [f"cannot validate: {exc}"]


def _valid(schema: Any, value: Any, root: Any) -> bool:
    """Check ``value`` against a sub-schema, resolving refs against the root document."""
    if isinstance(schema, dict) and isinstance(root, dict) and schema is not root:
        wrapped = dict(schema)
        for key in ("$defs", "definitions"):
            if key in root and key not in wrapped:
                wrapped[key] = root[key]
        if "$schema" in root and "$schema" not in wrapped:
            wrapped["$schema"] = root["$schema"]
        schema = wrapped
    return not _errors(schema, value)


class _Generator:
    def __init__(self, root: Any, include_optional: bool):
        self.root = root
        self.include_optional = include_optional

    # dispatch -----------------------------------------------------------
    def value(self, schema: Any, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise Unsatisfiable("schema nests too deeply (recursive $ref?)")
        if schema is True or schema == {}:
            return "sample"
        if schema is False:
            raise Unsatisfiable("schema is `false`, no value is allowed")
        if not isinstance(schema, dict):
            raise Unsatisfiable(f"not a schema: {schema!r}")

        if "$ref" in schema:
            target = self.resolve(schema["$ref"])
            rest = {k: v for k, v in schema.items() if k != "$ref"}
            schema = _merge(target, rest) if rest else target
            return self.value(schema, depth + 1)

        if "allOf" in schema:
            merged = {k: v for k, v in schema.items() if k != "allOf"}
            for part in schema["allOf"]:
                if isinstance(part, dict) and "$ref" in part:
                    part = self.resolve(part["$ref"])
                merged = _merge(merged, part)
            return self.value(merged, depth + 1)

        for key in ("oneOf", "anyOf"):
            if key in schema:
                base = {k: v for k, v in schema.items() if k != key}
                last: Optional[Exception] = None
                options = sorted(schema[key], key=_null_last)
                for option in options:
                    if isinstance(option, dict) and "$ref" in option:
                        option = self.resolve(option["$ref"])
                    candidate_schema = _merge(base, option) if isinstance(option, dict) else option
                    try:
                        candidate = self.value(candidate_schema, depth + 1)
                    except Unsatisfiable as exc:
                        last = exc
                        continue
                    if _valid(schema, candidate, self.root):
                        return candidate
                raise Unsatisfiable(f"no {key} branch produced a valid value" + (f" ({last})" if last else ""))

        if "const" in schema:
            return copy.deepcopy(schema["const"])
        if "enum" in schema and schema["enum"]:
            return copy.deepcopy(_first_non_null(schema["enum"]))
        for key in ("default",):
            if key in schema and _valid(schema, schema[key], self.root):
                return copy.deepcopy(schema[key])
        examples = schema.get("examples")
        if isinstance(examples, list):
            for ex in examples:
                if _valid(schema, ex, self.root):
                    return copy.deepcopy(ex)

        types = schema.get("type")
        if isinstance(types, str):
            types = [types]
        if not types:
            types = [_infer_type(schema)]
        types = sorted(types, key=lambda t: t == "null")
        last_exc: Optional[Exception] = None
        for t in types:
            try:
                return self.of_type(t, schema, depth)
            except Unsatisfiable as exc:
                last_exc = exc
        raise last_exc or Unsatisfiable("no usable type")

    def resolve(self, ref: str) -> Any:
        if not isinstance(ref, str) or not ref.startswith("#"):
            raise Unsatisfiable(f"external $ref not supported: {ref}")
        node: Any = self.root
        pointer = ref[1:]
        if pointer in ("", "/"):
            return node
        for part in pointer.lstrip("/").split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise Unsatisfiable(f"unresolvable $ref: {ref}")
        return node

    def of_type(self, t: str, schema: Dict[str, Any], depth: int) -> Any:
        if t == "object":
            return self.obj(schema, depth)
        if t == "array":
            return self.arr(schema, depth)
        if t == "string":
            return _string(schema)
        if t == "integer":
            return _number(schema, integer=True)
        if t == "number":
            return _number(schema, integer=False)
        if t == "boolean":
            return False
        if t == "null":
            return None
        raise Unsatisfiable(f"unknown type {t!r}")

    def obj(self, schema: Dict[str, Any], depth: int) -> Dict[str, Any]:
        props = schema.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        required = [r for r in (schema.get("required") or []) if isinstance(r, str)]
        names = list(required)
        if self.include_optional:
            names += [p for p in props if p not in names]
        min_props = schema.get("minProperties", 0)
        if isinstance(min_props, int) and len(names) < min_props:
            names += [p for p in props if p not in names][: min_props - len(names)]
        extra = schema.get("additionalProperties", True)
        out: Dict[str, Any] = {}
        for name in names:
            if name in props:
                sub = props[name]
            else:
                pattern_schema = _pattern_property(schema, name)
                if pattern_schema is not None:
                    sub = pattern_schema
                elif extra is False:
                    raise Unsatisfiable(f"'{name}' is required but additionalProperties is false and it has no schema")
                else:
                    sub = extra if isinstance(extra, dict) else {}
            out[name] = self.value(sub, depth + 1)
        filler = 0
        while isinstance(min_props, int) and len(out) < min_props:
            if extra is False:
                raise Unsatisfiable("minProperties cannot be met")
            key = f"key{filler}"
            filler += 1
            if key not in out:
                out[key] = self.value(extra if isinstance(extra, dict) else {}, depth + 1)
        return out

    def arr(self, schema: Dict[str, Any], depth: int) -> List[Any]:
        min_items = schema.get("minItems", 0)
        max_items = schema.get("maxItems")
        min_items = min_items if isinstance(min_items, int) else 0
        count = max(min_items, 1)
        if isinstance(max_items, int):
            count = min(count, max_items)
        prefix = schema.get("prefixItems")
        items = schema.get("items", {})
        if isinstance(items, list):  # draft-04..2019-09 tuple form
            prefix, items = items, schema.get("additionalItems", {})
        out: List[Any] = []
        for i in range(count):
            if isinstance(prefix, list) and i < len(prefix):
                sub = prefix[i]
            else:
                sub = items if items is not None else {}
            try:
                item = self.value(sub, depth + 1)
            except Unsatisfiable:
                if i >= min_items:
                    break
                raise
            if schema.get("uniqueItems") and item in out:
                item = _vary(item, i)
            out.append(item)
        if isinstance(prefix, list):
            while len(out) < min(len(prefix), count):
                out.append(self.value(prefix[len(out)], depth + 1))
        return out


def _merge(a: Dict[str, Any], b: Any) -> Dict[str, Any]:
    if not isinstance(b, dict):
        return dict(a)
    out = dict(a)
    for key, val in b.items():
        if key == "properties" and isinstance(out.get(key), dict) and isinstance(val, dict):
            merged = dict(out[key])
            for pname, pschema in val.items():
                if pname in merged and isinstance(merged[pname], dict) and isinstance(pschema, dict):
                    merged[pname] = _merge(merged[pname], pschema)
                else:
                    merged[pname] = pschema
            out[key] = merged
        elif key == "required" and isinstance(out.get(key), list) and isinstance(val, list):
            out[key] = list(dict.fromkeys(out[key] + val))
        elif key in ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties") and key in out:
            try:
                out[key] = max(out[key], val)
            except TypeError:
                out[key] = val
        elif key in ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties") and key in out:
            try:
                out[key] = min(out[key], val)
            except TypeError:
                out[key] = val
        else:
            out[key] = val
    return out


def _null_last(option: Any) -> int:
    return 1 if isinstance(option, dict) and option.get("type") == "null" else 0


def _first_non_null(values: List[Any]) -> Any:
    for v in values:
        if v is not None:
            return v
    return values[0]


def _infer_type(schema: Dict[str, Any]) -> str:
    keys = set(schema)
    if keys & {"properties", "required", "additionalProperties", "patternProperties", "minProperties"}:
        return "object"
    if keys & {"items", "prefixItems", "minItems", "maxItems", "uniqueItems"}:
        return "array"
    if keys & {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}:
        return "number"
    return "string"


def _pattern_property(schema: Dict[str, Any], name: str) -> Any:
    pp = schema.get("patternProperties")
    if isinstance(pp, dict):
        for pattern, sub in pp.items():
            try:
                if re.search(pattern, name):
                    return sub
            except re.error:
                continue
    return None


def _vary(item: Any, i: int) -> Any:
    if isinstance(item, str):
        return f"{item}{i}"
    if isinstance(item, bool):
        return not item
    if isinstance(item, (int, float)):
        return item + i
    return item


# strings ---------------------------------------------------------------

def _string(schema: Dict[str, Any]) -> str:
    min_len = schema.get("minLength", 0) if isinstance(schema.get("minLength", 0), int) else 0
    max_len = schema.get("maxLength") if isinstance(schema.get("maxLength"), int) else None
    pattern = schema.get("pattern") if isinstance(schema.get("pattern"), str) else None
    fmt = schema.get("format")

    candidates: List[str] = []
    if isinstance(fmt, str) and fmt in FORMAT_SAMPLES:
        candidates.append(FORMAT_SAMPLES[fmt])
    if pattern:
        for target in (min_len, max(min_len, 1)):
            built = _from_regex(pattern, target)
            if built is not None:
                candidates.append(built)
    candidates += STRING_CANDIDATES

    for cand in candidates:
        cand = _fit_length(cand, min_len, max_len, keep=bool(pattern) or bool(fmt))
        if cand is None:
            continue
        if pattern and not _matches(pattern, cand):
            continue
        return cand
    why = f"pattern {pattern!r}" if pattern else "length limits"
    raise Unsatisfiable(f"could not build a string that matches {why}")


def _fit_length(s: str, min_len: int, max_len: Optional[int], keep: bool) -> Optional[str]:
    if len(s) < min_len:
        if keep:
            return None
        s = s + "x" * (min_len - len(s))
    if max_len is not None and len(s) > max_len:
        if keep:
            return None
        s = s[:max_len]
    return s


def _matches(pattern: str, s: str) -> bool:
    try:
        return re.search(pattern, s) is not None
    except re.error:
        return False


def _from_regex(pattern: str, min_len: int) -> Optional[str]:
    """Build a short string that matches ``pattern`` by walking its parse tree."""
    try:
        parsed = sre_parse.parse(pattern)
    except Exception:
        return None
    try:
        out = _emit(parsed, max(min_len, 0))
    except _Give_up:
        return None
    return out if _matches(pattern, out) else None


class _Give_up(Exception):
    pass


def _emit(parsed: Any, want: int) -> str:
    parts: List[str] = []
    items = list(parsed)
    for idx, (op, av) in enumerate(items):
        name = str(op)
        if name == "LITERAL":
            parts.append(chr(av))
        elif name == "NOT_LITERAL":
            parts.append("a" if chr(av) != "a" else "b")
        elif name == "ANY":
            parts.append("a")
        elif name == "IN":
            parts.append(_from_set(av))
        elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            lo, hi, sub = av
            n = lo
            # Stretch the last open-ended repeat so the whole string reaches minLength.
            if want and hi != lo:
                current = len("".join(parts))
                rest = len(_emit(items[idx + 1:], 0))
                one = len(_emit(sub, 0)) or 1
                need = want - current - rest - lo * one
                if need > 0:
                    extra = math.ceil(need / one)
                    n = lo + extra
                    if hi is not sre_constants.MAXREPEAT and isinstance(hi, int):
                        n = min(n, hi)
            parts.append(_emit(sub, 0) * n)
        elif name == "SUBPATTERN":
            sub = av[-1]
            parts.append(_emit(sub, 0))
        elif name == "BRANCH":
            parts.append(_emit(av[1][0], 0))
        elif name in ("AT", "ASSERT", "ASSERT_NOT"):
            continue
        elif name == "CATEGORY":
            parts.append(_from_category(av))
        elif name == "GROUPREF":
            raise _Give_up()
        else:
            raise _Give_up()
    return "".join(parts)


def _from_set(items: Any) -> str:
    negate = False
    for op, av in items:
        name = str(op)
        if name == "NEGATE":
            negate = True
            continue
        if negate:
            break
        if name == "LITERAL":
            return chr(av)
        if name == "RANGE":
            return chr(av[0])
        if name == "CATEGORY":
            return _from_category(av)
    if negate:
        for ch in "a0_A":
            if not _set_contains(items, ch):
                return ch
    raise _Give_up()


def _set_contains(items: Any, ch: str) -> bool:
    for op, av in items:
        name = str(op)
        if name == "LITERAL" and chr(av) == ch:
            return True
        if name == "RANGE" and av[0] <= ord(ch) <= av[1]:
            return True
        if name == "CATEGORY" and _from_category(av) == ch:
            return True
    return False


def _from_category(cat: Any) -> str:
    name = str(cat)
    if "NOT_DIGIT" in name:
        return "a"
    if "DIGIT" in name:
        return "0"
    if "NOT_WORD" in name:
        return "-"
    if "WORD" in name:
        return "a"
    if "NOT_SPACE" in name:
        return "a"
    if "SPACE" in name:
        return " "
    return "a"


# numbers ---------------------------------------------------------------

def _number(schema: Dict[str, Any], integer: bool) -> Any:
    lo = _num(schema.get("minimum"))
    hi = _num(schema.get("maximum"))
    xlo = schema.get("exclusiveMinimum")
    xhi = schema.get("exclusiveMaximum")
    # draft-04 boolean form
    if xlo is True and lo is not None:
        xlo, lo = lo, None
    if xhi is True and hi is not None:
        xhi, hi = hi, None
    xlo = _num(xlo)
    xhi = _num(xhi)
    step = _num(schema.get("multipleOf"))

    value: float = 1
    if lo is not None and value < lo:
        value = lo
    if xlo is not None and value <= xlo:
        value = xlo + (1 if integer or step else 0.5)
    if integer:
        value = math.ceil(value)
    if step:
        value = math.ceil(value / step) * step
        if xlo is not None and value <= xlo:
            value += step
    if hi is not None and value > hi:
        value = hi
    if xhi is not None and value >= xhi:
        value = xhi - (1 if integer else 0.5)
    if step:
        value = math.floor(value / step) * step
    if integer:
        value = math.floor(value)
    if (lo is not None and value < lo) or (hi is not None and value > hi) \
            or (xlo is not None and value <= xlo) or (xhi is not None and value >= xhi):
        raise Unsatisfiable("no number fits the minimum/maximum/multipleOf limits")
    if integer or float(value).is_integer():
        return int(value)
    return float(value)


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    return None

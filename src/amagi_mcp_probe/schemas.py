"""JSON Schema helpers: pick the right draft, check a schema against its metaschema."""

from __future__ import annotations

import re
import warnings
from typing import Any, Iterator, List, Optional, Tuple

from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for

# MCP 2025-11-25: schemas without "$schema" are JSON Schema 2020-12.
DEFAULT_VALIDATOR = Draft202012Validator


def validator_class(schema: Any) -> Tuple[Any, Optional[str]]:
    """Return (validator class, warning) for a schema, honouring its $schema."""
    note = None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cls = validator_for(schema, default=DEFAULT_VALIDATOR)
    if caught and isinstance(schema, dict) and "$schema" in schema:
        note = f"unknown $schema {schema['$schema']!r}; checked as JSON Schema 2020-12"
    return cls, note


def make_validator(schema: Any):
    cls, _ = validator_class(schema)
    return cls(schema)


def schema_problems(schema: Any) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings) for a schema. Errors mean the schema itself is invalid."""
    errors: List[str] = []
    notes: List[str] = []
    cls, note = validator_class(schema)
    if note:
        notes.append(note)
    meta = cls(cls.META_SCHEMA)
    for err in sorted(meta.iter_errors(schema), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{where}: {err.message}")
        if len(errors) >= 5:
            break
    if errors:
        return errors, notes
    for where, pattern in _patterns(schema, ""):
        try:
            re.compile(pattern)
        except re.error as exc:
            # JSON Schema regexes are ECMA-262; Python's re rejects a few valid ones (\p{L}, ...).
            notes.append(f"{where or '(root)'}: regex {pattern!r} does not compile in Python ({exc}); "
                         "check it is valid ECMA-262")
    # A metaschema-valid schema can still break at validation time (bad $ref, bad regex).
    try:
        validator = cls(schema)
        list(validator.iter_errors({}))
    except re.error:
        pass  # already reported above as a note
    except Exception as exc:  # referencing.exceptions.Unresolvable, ...
        errors.append(f"schema cannot be used for validation: {type(exc).__name__}: {exc}")
    return errors, notes


def _patterns(node: Any, where: str) -> Iterator[Tuple[str, str]]:
    """Yield (location, regex) for every pattern and patternProperties key in a schema."""
    if isinstance(node, dict):
        if isinstance(node.get("pattern"), str):
            yield where, node["pattern"]
        pp = node.get("patternProperties")
        if isinstance(pp, dict):
            for key in pp:
                yield f"{where}/patternProperties".lstrip("/"), key
        for key, val in node.items():
            if key in ("const", "enum", "default", "examples"):
                continue
            yield from _patterns(val, f"{where}/{key}".lstrip("/"))
    elif isinstance(node, list):
        for i, val in enumerate(node):
            yield from _patterns(val, f"{where}/{i}".lstrip("/"))

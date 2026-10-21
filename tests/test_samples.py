import pytest
from jsonschema import Draft7Validator, Draft202012Validator

from amagi_mcp_probe.samples import Unsatisfiable, generate

CASES = [
    {"type": "object"},
    {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    {"type": "object", "properties": {"n": {"type": "integer", "minimum": 5, "maximum": 9}}, "required": ["n"]},
    {"type": "object", "properties": {"n": {"type": "integer", "exclusiveMinimum": 10}}, "required": ["n"]},
    {"type": "object", "properties": {"n": {"type": "number", "maximum": -3.5}}, "required": ["n"]},
    {"type": "object", "properties": {"n": {"type": "integer", "multipleOf": 7, "minimum": 20}}, "required": ["n"]},
    {"type": "object", "properties": {"x": {"type": "number", "multipleOf": 0.25, "exclusiveMinimum": 1}},
     "required": ["x"]},
    {"type": "object", "properties": {"color": {"enum": ["red", "green"]}}, "required": ["color"]},
    {"type": "object", "properties": {"kind": {"const": "fixed"}}, "required": ["kind"]},
    {"type": "object", "properties": {"s": {"type": "string", "minLength": 12, "maxLength": 14}}, "required": ["s"]},
    {"type": "object", "properties": {"id": {"type": "string", "pattern": "^note-[0-9]{3}$"}}, "required": ["id"]},
    {"type": "object", "properties": {"sku": {"type": "string", "pattern": "^[A-Z]{3}-\d{4,6}$"}},
     "required": ["sku"]},
    {"type": "object", "properties": {"v": {"type": "string", "pattern": "^(alpha|beta)\.[a-f0-9]+$"}},
     "required": ["v"]},
    {"type": "object", "properties": {"code": {"type": "string", "pattern": "[xyz]{2}", "minLength": 6}},
     "required": ["code"]},
    {"type": "object", "properties": {"email": {"type": "string", "format": "email"}}, "required": ["email"]},
    {"type": "object", "properties": {"when": {"type": "string", "format": "date-time"}}, "required": ["when"]},
    {"type": "object", "properties": {"u": {"type": "string", "format": "uri"}}, "required": ["u"]},
    {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"}}, "required": ["id"]},
    {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}, "minItems": 3,
                                               "uniqueItems": True}}, "required": ["tags"]},
    {"type": "object", "properties": {"pt": {"type": "array", "prefixItems": [{"type": "number"},
                                                                              {"type": "string"}],
                                             "minItems": 2}}, "required": ["pt"]},
    {"type": "object", "properties": {"opt": {"type": ["string", "null"]}}, "required": ["opt"]},
    {"type": "object", "properties": {"a": {"anyOf": [{"type": "null"}, {"type": "integer", "minimum": 3}]}},
     "required": ["a"]},
    {"type": "object", "properties": {"o": {"oneOf": [{"type": "string", "maxLength": 1},
                                                      {"type": "integer"}]}}, "required": ["o"]},
    {"type": "object", "allOf": [{"properties": {"a": {"type": "string"}}, "required": ["a"]},
                                 {"properties": {"b": {"type": "boolean"}}, "required": ["b"]}]},
    {"type": "object", "$defs": {"pos": {"type": "integer", "minimum": 1}},
     "properties": {"p": {"$ref": "#/$defs/pos"}}, "required": ["p"]},
    {"type": "object", "definitions": {"node": {"type": "object", "properties": {
        "name": {"type": "string"}, "children": {"type": "array", "items": {"$ref": "#/definitions/node"}}},
        "required": ["name"]}}, "properties": {"root": {"$ref": "#/definitions/node"}}, "required": ["root"]},
    {"type": "object", "properties": {"meta": {"type": "object", "minProperties": 2,
                                               "additionalProperties": {"type": "integer"}}},
     "required": ["meta"]},
    {"type": "object", "patternProperties": {"^x-[a-z]+$": {"type": "string"}}, "minProperties": 1,
     "additionalProperties": False},
    {"type": "object", "properties": {"limit": {"type": "integer", "default": 10, "minimum": 1}},
     "required": ["limit"]},
    {"type": "object", "properties": {"d": {"type": "string", "default": "bad", "minLength": 5}},
     "required": ["d"]},
    {"type": "object", "properties": {"path": {"type": "string", "examples": ["/tmp/a.txt"]}}, "required": ["path"]},
    {"type": "object", "properties": {"flag": {"type": "boolean"}, "n": {"type": "null"}},
     "required": ["flag", "n"]},
    {"type": "object", "properties": {"nested": {"properties": {"k": {"minimum": 2}}, "required": ["k"]}},
     "required": ["nested"]},
]


@pytest.mark.parametrize("schema", CASES)
def test_generated_arguments_validate(schema):
    value = generate(schema)
    errors = [e.message for e in Draft202012Validator(schema).iter_errors(value)]
    assert errors == [], (value, errors)


@pytest.mark.parametrize("schema", CASES)
def test_include_optional_still_validates(schema):
    value = generate(schema, include_optional=True)
    assert list(Draft202012Validator(schema).iter_errors(value)) == []


def test_required_only_by_default():
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
              "required": ["a"]}
    assert set(generate(schema)) == {"a"}
    assert set(generate(schema, include_optional=True)) == {"a", "b"}


def test_small_numbers_and_first_enum():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}, "e": {"enum": ["b", "a"]}},
              "required": ["n", "e"]}
    assert generate(schema) == {"n": 1, "e": "b"}


def test_pattern_sample_matches():
    out = generate({"type": "object", "properties": {"id": {"type": "string", "pattern": "^note-[0-9]{3}$"}},
                    "required": ["id"]})
    assert out["id"].startswith("note-") and len(out["id"]) == 8


def test_valid_default_is_used():
    schema = {"type": "object", "properties": {"limit": {"type": "integer", "default": 25}}, "required": ["limit"]}
    assert generate(schema) == {"limit": 25}


def test_draft7_exclusive_bounds():
    schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
              "properties": {"n": {"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 3}},
              "required": ["n"]}
    value = generate(schema)
    assert list(Draft7Validator(schema).iter_errors(value)) == []


def test_unsatisfiable_raises():
    with pytest.raises(Unsatisfiable):
        generate({"type": "object", "properties": {"n": {"type": "integer", "minimum": 5, "maximum": 2}},
                  "required": ["n"]})
    with pytest.raises(Unsatisfiable):
        generate({"not": {}})


def test_external_ref_is_unsatisfiable():
    with pytest.raises(Unsatisfiable):
        generate({"type": "object", "properties": {"a": {"$ref": "https://example.com/s.json"}}, "required": ["a"]})

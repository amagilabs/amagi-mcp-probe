from amagi_mcp_probe.probe import result_problems
from amagi_mcp_probe.schemas import schema_problems


def test_valid_schema_has_no_problems():
    schema = {"type": "object", "properties": {"q": {"type": "string", "pattern": "^[a-z]+$"}}}
    assert schema_problems(schema) == ([], [])


def test_bad_type_is_an_error():
    errors, _ = schema_problems({"type": "object", "properties": {"n": {"type": "int"}}})
    assert errors and errors[0].startswith("properties/n/type:")


def test_bad_keyword_value_is_an_error():
    errors, _ = schema_problems({"type": "object", "required": "q"})
    assert errors


def test_unresolvable_ref_is_an_error():
    errors, _ = schema_problems({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}})
    assert errors == ["properties/a: $ref '#/$defs/missing' points at nothing"]
    good = {"type": "object", "$defs": {"x": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/x"}}}
    assert schema_problems(good) == ([], [])


def test_ecma_only_regex_is_a_note_not_an_error():
    errors, notes = schema_problems({"type": "object", "properties": {"n": {"type": "string",
                                                                             "pattern": "^\\p{L}+$"}}})
    assert errors == []
    assert notes and "ECMA-262" in notes[0]


def test_unknown_schema_dialect_is_a_note():
    errors, notes = schema_problems({"$schema": "https://example.com/my-dialect", "type": "object"})
    assert errors == []
    assert notes and "unknown $schema" in notes[0]


def test_draft7_schema_is_checked_as_draft7():
    schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
              "properties": {"n": {"type": "integer", "exclusiveMinimum": 0}}}
    assert schema_problems(schema) == ([], [])


def test_good_results():
    assert result_problems({"content": []}) == []
    assert result_problems({"content": [
        {"type": "text", "text": "hi"},
        {"type": "image", "data": "AAAA", "mimeType": "image/png"},
        {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"},
        {"type": "resource_link", "uri": "file:///a", "name": "a"},
        {"type": "resource", "resource": {"uri": "file:///a", "text": "x"}},
        {"type": "resource", "resource": {"uri": "file:///b", "blob": "AAAA"}},
    ], "isError": False, "structuredContent": {"a": 1}}) == []


def test_bad_results():
    assert result_problems(None) == ["result is NoneType, not an object"]
    assert result_problems({}) == ["result has no `content` array"]
    probs = result_problems({"content": ["x", {"text": "no type"}, {"type": "resource_link", "uri": "u"}],
                             "isError": 1, "structuredContent": "s"})
    assert "content[0] is not an object" in probs
    assert "content[1] has no `type`" in probs
    assert "content[2] type resource_link needs a string `name`" in probs
    assert "`isError` is not a boolean" in probs
    assert "`structuredContent` is not an object" in probs

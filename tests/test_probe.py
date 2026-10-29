"""End-to-end probe runs against real stdio server processes."""

import os

from conftest import fixture_cmd

from amagi_mcp_probe.probe import Options, Prober, shorten_paths
from amagi_mcp_probe.transport import StdioTransport


def run(cmd, **opts):
    opts.setdefault("timeout", 5)
    opts.setdefault("startup_timeout", 20)
    return Prober("t", lambda: StdioTransport(cmd), Options(**opts), "stdio").run()


def checks(result, tool=None):
    if tool is None:
        return {f.check for f in result.findings}
    matches = [t for t in result.tools if t.name == tool]
    assert matches, f"no tool {tool!r}"
    return {f.check for t in matches for f in t.findings}


def status(result):
    return {t.name: t.status for t in result.tools}


def test_well_behaved_server_passes(notes_cmd):
    r = run(notes_cmd)
    assert r.server_info == {"name": "notes", "version": "1.0.0"}
    assert r.protocol_version == "2025-11-25"
    assert r.findings == []
    assert status(r) == {"add_note": "pass", "get_note": "warn", "search_notes": "pass"}
    # get_note gets a well-formed id that does not exist, so the server answers isError.
    assert checks(r, "get_note") == {"tool-error"}
    assert not r.failed()
    assert r.failed(strict=True)


def test_generated_arguments_match_schema(notes_cmd):
    r = run(notes_cmd)
    args = {t.name: t.arguments for t in r.tools}
    assert args["get_note"]["id"].startswith("note-")
    assert set(args["add_note"]) == {"title"}
    assert all(t.args_source == "generated" for t in r.tools)


def test_args_override_makes_get_note_pass(notes_cmd):
    r = run(notes_cmd, args={"get_note": {"id": "note-001"}})
    assert status(r)["get_note"] == "pass"
    assert r.tools[1].args_source == "--args"
    assert not r.failed(strict=True)


def test_args_override_that_breaks_schema_is_flagged(notes_cmd):
    r = run(notes_cmd, args={"get_note": [{"id": "note-001"}, {"id": 7}]}, only=["get_note"])
    assert "args-override-invalid" in checks(r, "get_note")


def test_buggy_server_findings(buggy_cmd):
    r = run(buggy_cmd, timeout=1.5)
    assert status(r) == {
        "echo": "pass",
        "resize_image": "fail",
        "fetch_page": "fail",
        "weather": "fail",
        "lookup_user": "fail",
        "slow_report": "fail",
        "count_rows": "fail",
        "drop_table": "skip",
    }
    assert checks(r, "resize_image") == {"server-crash"}
    crash = r.tools[1].findings[0]
    assert any("ZeroDivisionError" in line for line in crash.detail)
    assert checks(r, "fetch_page") == {"stdout-noise"}
    assert checks(r, "weather") == {"structured-content-invalid"}
    assert checks(r, "lookup_user") == {"call-rpc-error"}
    assert checks(r, "slow_report") == {"call-timeout", "tool-description"}
    assert checks(r, "count_rows") == {"input-schema-invalid"}
    assert r.tools[-1].skip_reason.startswith("destructiveHint")
    assert r.restarts == 2  # once after the crash, once after the hang
    assert checks(r) == set()  # the stdout noise is blamed on fetch_page, not the server


def test_allow_destructive_calls_the_tool(buggy_cmd):
    r = run(buggy_cmd, only=["drop_*"], allow_destructive=True)
    assert status(r)["drop_table"] == "pass"
    assert status(r)["echo"] == "skip"


def test_no_call_checks_schemas_only(buggy_cmd):
    r = run(buggy_cmd, call=False)
    assert status(r)["count_rows"] == "fail"  # a broken schema still fails
    assert status(r)["echo"] == "skip"
    assert all(t.duration_ms is None for t in r.tools)
    assert r.restarts == 0


def test_ignore_drops_a_check(buggy_cmd):
    r = run(buggy_cmd, only=["slow_report"], timeout=1, ignore={"tool-description", "call-timeout"})
    assert status(r)["slow_report"] == "pass"


def test_restart_limit(buggy_cmd):
    r = run(buggy_cmd, only=["resize_image", "slow_report", "drop_table"], allow_destructive=True,
            timeout=1, max_restarts=1)
    assert r.restarts == 1
    assert "too-many-restarts" in checks(r)
    assert status(r)["drop_table"] == "not-run"
    assert status(r)["echo"] == "skip"


def test_definition_and_result_problems():
    r = run(fixture_cmd("edge_server.py", "paged"))
    names = [t.name for t in r.tools]
    assert len(names) == 9, "both pages of tools/list are read"
    assert checks(r, "bad name!") == {"tool-name"}
    assert r.tools[3].name == "no_content" and {f.check for f in r.tools[3].findings} >= {"tool-name-duplicate"}
    assert "call-result-invalid" in checks(r, "no_content")
    odd = [f.message for f in r.tools[1].findings if f.check == "call-result-invalid"]
    assert any("needs a string `text`" in m for m in odd)
    assert any("needs a string `mimeType`" in m for m in odd)
    assert any("resource.text" in m for m in odd)
    assert any("structuredContent" in m for m in odd)
    assert "content-type-unknown" in checks(r, "odd_blocks")
    assert checks(r, "string_schema") == {"input-schema-type"}
    assert status(r)["string_schema"] == "fail"
    assert checks(r, "ghost_required") == {"required-not-defined"}
    assert checks(r, "missing_structured") == {"structured-content-missing"}
    assert status(r)["needs_task"] == "skip"
    assert checks(r, "fails_politely") == {"tool-error"}
    assert "database is offline" in r.tools[-1].findings[0].message


def test_pagination_loop_is_reported():
    r = run(fixture_cmd("edge_server.py", "loop"))
    assert "tools-list-error" in checks(r)
    assert "loops" in r.findings[0].message
    assert len(r.tools) == 2  # the first page, then the page for the repeated cursor


def test_server_that_exits_at_startup():
    r = run(fixture_cmd("edge_server.py", "exit"))
    assert checks(r) == {"initialize-error"}
    f = r.findings[0]
    assert "code 3" in f.message
    assert any("API_KEY is not set" in line for line in f.detail)
    assert r.failed()


def test_server_that_never_answers_initialize():
    r = run(fixture_cmd("edge_server.py", "silent"), startup_timeout=1)
    assert checks(r) == {"initialize-timeout"}


def test_command_not_found():
    r = run(["amagi-no-such-command-xyz", "--flag"])
    assert checks(r) == {"server-start"}


def test_protocol_version_is_negotiated(notes_cmd):
    r = run(notes_cmd, protocol_version="2025-06-18", call=False)
    assert r.protocol_version == "2025-06-18"
    assert r.findings == []



def test_shorten_paths():
    base = os.path.join(os.path.abspath(os.sep), "work", "proj")
    line = '  File "' + os.path.join(base, "srv", "main.py") + '", line 3'
    assert shorten_paths([line], [base]) == ['  File "' + os.path.join("srv", "main.py") + '", line 3']
    assert shorten_paths([base.replace(os.sep, "/") + "/a.py"], [base]) == ["a.py"]
    assert shorten_paths(["/etc/x"], [os.path.abspath(os.sep)]) == ["/etc/x"]  # a bare root is never stripped


def test_crash_detail_has_no_absolute_paths(buggy_cmd):
    r = run(buggy_cmd, timeout=10, only=["resize_image"])  # it crashes; no timeout needed
    detail = next(t for t in r.tools if t.name == "resize_image").findings[0].detail
    assert any("buggy_server.py" in line for line in detail)
    assert not any(os.getcwd().lower() in line.lower() for line in detail)

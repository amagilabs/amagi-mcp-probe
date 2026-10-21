import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from amagi_mcp_probe import __version__
from amagi_mcp_probe.cli import main, server_label
from amagi_mcp_probe.config import ConfigError, expand, load_args, load_servers


def cli(capsys, *argv):
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_notes_server_passes(capsys, notes_cmd):
    rc, out, _ = cli(capsys, "--", *notes_cmd)
    assert rc == 0
    assert "notes 1.0.0, protocol 2025-11-25" in out
    assert out.rstrip().endswith("3 tools: 2 passed, 1 warned.  PASS")


def test_strict_fails_on_warnings(capsys, notes_cmd):
    assert cli(capsys, "--strict", "--", *notes_cmd)[0] == 1
    assert cli(capsys, "--strict", "--ignore", "tool-error", "--", *notes_cmd)[0] == 0


def test_buggy_server_fails_with_json(capsys, buggy_cmd):
    rc, out, _ = cli(capsys, "--timeout", "1.5", "--format", "json", "--", *buggy_cmd)
    assert rc == 1
    doc = json.loads(out)
    assert doc["verdict"] == "FAIL"
    server = doc["servers"][0]
    assert server["counts"] == {"pass": 1, "warn": 0, "fail": 6, "skip": 1, "not-run": 0}
    assert server["restarts"] == 2


def test_github_annotations(capsys, buggy_cmd):
    rc, out, _ = cli(capsys, "--format", "github", "--no-call", "--", *buggy_cmd)
    assert rc == 1
    assert "::error title=amagi-mcp-probe::buggy_server.py / count_rows: input-schema-invalid:" in out
    assert "::warning title=amagi-mcp-probe::buggy_server.py / slow_report: tool-description:" in out


def test_junit_and_summary_files(capsys, tmp_path, notes_cmd):
    junit = tmp_path / "probe.xml"
    summary = tmp_path / "summary.md"
    summary.write_text("# earlier step\n", encoding="utf-8")
    rc, _, _ = cli(capsys, "--junit", str(junit), "--summary", str(summary), "--", *notes_cmd)
    assert rc == 0
    root = ET.parse(junit).getroot()
    assert root.tag == "testsuites"
    assert root.get("tests") == "4" and root.get("failures") == "0"
    names = [c.get("name") for c in root.iter("testcase")]
    assert names == ["server", "tool add_note", "tool get_note", "tool search_notes"]
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("# earlier step\n")  # appended, not overwritten
    assert "## amagi-mcp-probe: PASS" in text
    assert "| `get_note` | WARN |" in text


def test_junit_failures(capsys, tmp_path, buggy_cmd):
    junit = tmp_path / "probe.xml"
    rc, _, _ = cli(capsys, "--no-call", "--junit", str(junit), "--", *buggy_cmd)
    assert rc == 1
    root = ET.parse(junit).getroot()
    failed = [c.get("name") for c in root.iter("testcase") if c.find("failure") is not None]
    assert failed == ["tool count_rows"]


def test_markdown_format(capsys, notes_cmd):
    rc, out, _ = cli(capsys, "--format", "markdown", "--", *notes_cmd)
    assert rc == 0 and out.startswith("## amagi-mcp-probe: PASS")


def test_verbose_shows_arguments(capsys, notes_cmd):
    _, out, _ = cli(capsys, "-v", "--only", "get_note", "--", *notes_cmd)
    assert 'args (generated): {"id": "note-' in out
    assert "SKIP  add_note" in out and "not matched by --only" in out


def test_args_file(capsys, tmp_path, notes_cmd):
    args = tmp_path / "args.json"
    args.write_text(json.dumps({"get_note": {"id": "note-001"}}), encoding="utf-8")
    rc, out, _ = cli(capsys, "--strict", "--args", str(args), "--", *notes_cmd)
    assert rc == 0, out
    assert "3 passed" in out


def test_config_file_and_server_filter(capsys, tmp_path, notes_cmd, buggy_cmd):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "notes": {"command": notes_cmd[0], "args": notes_cmd[1:]},
        "buggy": {"command": buggy_cmd[0], "args": buggy_cmd[1:]},
    }}), encoding="utf-8")
    rc, out, _ = cli(capsys, "--config", str(cfg), "--server", "notes")
    assert rc == 0
    assert out.startswith("notes: ")
    rc, out, _ = cli(capsys, "--config", str(cfg), "--no-call")
    assert rc == 1
    assert "2 servers, 1 failed.  FAIL" in out


def test_list_checks(capsys):
    rc, out, _ = cli(capsys, "--list-checks")
    assert rc == 0
    assert "server-crash" in out and "structured-content-invalid" in out


@pytest.mark.parametrize("argv, message", [
    ([], "give a server command"),
    (["--url", "http://x/mcp", "--", "python", "s.py"], "only one of"),
    (["--ignore", "nope", "--", "python", "s.py"], "unknown check id"),
    (["--timeout", "0", "--", "python", "s.py"], "timeouts must be positive"),
    (["--env", "NOEQUALS", "--", "python", "s.py"], "KEY=VALUE"),
    (["--config", "does-not-exist.json"], "file not found"),
])
def test_usage_errors(capsys, argv, message):
    rc, _, err = cli(capsys, *argv)
    assert rc == 2
    assert message in err


def test_unknown_config_server(capsys, tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8")
    rc, _, err = cli(capsys, "--config", str(cfg), "--server", "b")
    assert rc == 2 and "not in" in err


def test_module_entry_point_and_version():
    out = subprocess.run([sys.executable, "-m", "amagi_mcp_probe", "--version"],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == f"amagi-mcp-probe {__version__}"


@pytest.mark.parametrize("command, label", [
    (["python", "examples/servers/buggy_server.py"], "buggy_server.py"),
    (["npx", "-y", "@modelcontextprotocol/server-everything"], "server-everything"),
    (["node", "/x/node_modules/@modelcontextprotocol/server-everything/dist/index.js", "stdio"],
     "server-everything"),
    (["uv", "run", "server.py"], "server.py"),
    (["python", "-m", "my_mcp"], "my_mcp"),
    (["my-server"], "my-server"),
    ([r"C:\tools\srv.exe", "--port", "1"], "srv.exe"),
])
def test_server_label(command, label):
    assert server_label(command) == label


# config ---------------------------------------------------------------------

def test_expand(monkeypatch):
    monkeypatch.setenv("PROBE_TOKEN", "abc")
    monkeypatch.delenv("PROBE_MISSING", raising=False)
    assert expand("Bearer ${PROBE_TOKEN}") == "Bearer abc"
    assert expand("${PROBE_MISSING:-fallback}") == "fallback"
    assert expand("${PROBE_MISSING}") == "${PROBE_MISSING}"


def test_load_servers_variants(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_TOKEN", "abc")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": {
        "local": {"type": "stdio", "command": "uvx", "args": ["my-server"], "env": {"K": "${PROBE_TOKEN}"},
                  "cwd": "sub"},
        "remote": {"type": "http", "url": "https://example.com/mcp",
                   "headers": {"Authorization": "Bearer ${PROBE_TOKEN}"}},
        "old": {"type": "sse", "url": "https://example.com/sse"},
    }}), encoding="utf-8")
    local, remote, old = load_servers(str(cfg))
    assert local.kind == "stdio" and local.command == ["uvx", "my-server"]
    assert local.env == {"K": "abc"}
    assert local.cwd == os.path.join(str(tmp_path), "sub")
    assert remote.kind == "http" and remote.headers == {"Authorization": "Bearer abc"}
    assert old.unsupported and "legacy" in old.unsupported


def test_load_servers_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_servers(str(bad))
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_servers(str(empty))
    nocmd = tmp_path / "nocmd.json"
    nocmd.write_text(json.dumps({"mcpServers": {"x": {"args": []}}}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_servers(str(nocmd))


def test_load_args(tmp_path):
    good = tmp_path / "a.json"
    good.write_text(json.dumps({"t": {"a": 1}, "u": [{"a": 1}, {"a": 2}]}), encoding="utf-8")
    assert load_args(str(good))["u"][1] == {"a": 2}
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps({"t": 5}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_args(str(bad))


def test_args_file_list_calls_each_entry(capsys, tmp_path, notes_cmd):
    args = tmp_path / "args.json"
    args.write_text(json.dumps({"get_note": [{"id": "note-001"}, {"id": "nope"}]}), encoding="utf-8")
    rc, out, _ = cli(capsys, "--only", "get_note", "--args", str(args), "--", *notes_cmd)
    assert rc == 0
    assert "args-override-invalid: --args (call 2) does not match inputSchema" in out
    assert "tool-error: isError: true: no note with id nope" in out

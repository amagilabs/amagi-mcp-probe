"""MCP 2026-07-28 era detection and the modern-era checks (stdlib fixture servers)."""

import json
import os
import sys

import pytest
from conftest import fixture_cmd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
import http_mcp  # noqa: E402

from amagi_mcp_probe import era  # noqa: E402
from amagi_mcp_probe.cli import main  # noqa: E402
from amagi_mcp_probe.probe import Options, Prober  # noqa: E402
from amagi_mcp_probe.report import render_json, render_markdown, render_text  # noqa: E402
from amagi_mcp_probe.transport import HttpTransport, StdioTransport, header_value  # noqa: E402

CLIENT = {"name": "test", "version": "0"}


def run(mode, **opts):
    opts.setdefault("timeout", 5)
    opts.setdefault("startup_timeout", 10)
    opts.setdefault("discover_timeout", 10)  # a busy machine can be slow to answer; tests that need a timeout pass 1.5
    cmd = fixture_cmd("era_server.py", mode)
    return Prober("e", lambda: StdioTransport(cmd), Options(**opts), "stdio").run()


def checks(result):
    return sorted(f.check for f in result.all_findings())


def era_connect(server, **kw):
    cmd = fixture_cmd("era_server.py", server)
    kw.setdefault("discover_timeout", 10)
    kw.setdefault("timeout", 10)
    kw.setdefault("use_cache", False)
    return era.connect(lambda: StdioTransport(cmd), CLIENT, **kw)


# connect() --------------------------------------------------------------------------

def test_modern_server_is_modern():
    t, hs = era_connect("modern")
    try:
        assert (hs.era, hs.protocol_version, hs.supported_versions) == ("modern", "2026-07-28", ["2026-07-28"])
        assert hs.server_info == {"name": "era-fixture", "version": "1.0"}
        assert hs.fallback is None and hs.initialize is None
        page = t.request("tools/list", {})
        assert page["resultType"] == "complete"
    finally:
        t.close()


def test_legacy_silent_falls_back_after_timeout():
    t, hs = era_connect("legacy-silent", discover_timeout=1.5)
    t.close()
    assert hs.era == "legacy" and hs.protocol_version == "2025-11-25"
    assert "no answer to server/discover" in hs.fallback


def test_legacy_crash_restarts_then_falls_back():
    t, hs = era_connect("legacy-crash")
    t.close()
    assert hs.era == "legacy" and hs.discover_crashed


def test_slow_legacy_crash_restarts_for_initialize(tmp_path):
    # discover times out while the server starts, then kills it when read: restart, initialize alone
    cmd = fixture_cmd("era_server.py", "legacy-crash-slow", str(tmp_path / "warm"), "2.5")
    t, hs = era.connect(lambda: StdioTransport(cmd), CLIENT, timeout=10, discover_timeout=1.5, use_cache=False)
    try:
        assert hs.era == "legacy" and hs.discover_crashed and "restarted it" in hs.fallback
        assert t.request("tools/list", {})["tools"][0]["name"] == "add"
    finally:
        t.close()


def test_slow_start_reprobes_after_initialize_says_modern():
    # discover times out, initialize answers -32022 naming 2026-07-28: try discover again
    t, hs = era_connect("slow-start", discover_timeout=1.5)
    t.close()
    assert hs.era == "modern" and hs.fallback is None


def test_lenient_legacy_server_is_legacy():
    # {} is not a DiscoverResult: fall back to initialize, which works
    t, hs = era_connect("legacy-lenient")
    t.close()
    assert hs.era == "legacy" and "without supportedVersions" in hs.fallback


def test_modern_server_with_bad_discover_is_still_modern():
    # discover has no supportedVersions, initialize says -32022: modern after all
    t, hs = era_connect("modern-baddiscover")
    t.close()
    assert hs.era == "modern" and hs.fallback is None


def test_slow_dual_server_is_restarted_modern(tmp_path):
    # the first start sleeps past the discover timeout; its late DiscoverResult means "modern"
    cmd = fixture_cmd("era_server.py", "dual-slow", str(tmp_path / "warm"), "2.5")
    t, hs = era.connect(lambda: StdioTransport(cmd), CLIENT, timeout=10, discover_timeout=1.5, use_cache=False)
    t.close()
    assert hs.era == "modern" and hs.late_discover and hs.fallback is None and hs.initialize is None


def test_probe_slow_dual_server(tmp_path):
    cmd = fixture_cmd("era_server.py", "dual-slow", str(tmp_path / "warm"), "2.5")
    r = Prober("e", lambda: StdioTransport(cmd), Options(timeout=5, startup_timeout=10, discover_timeout=1.5),
               "stdio").run()
    assert r.era == "dual" and checks(r) == []
    assert "slow start" in r.era_note


def test_modern_without_discover_is_detected():
    t, hs = era_connect("modern-nodiscover")
    t.close()
    assert hs.era == "modern" and hs.discover_missing


def test_forced_eras():
    t, hs = era_connect("dual", mode="legacy")
    t.close()
    assert hs.era == "legacy" and hs.discover is None
    t, hs = era_connect("dual", mode="modern")
    t.close()
    assert hs.era == "modern" and hs.initialize is None
    with pytest.raises(era.ConnectError):
        era_connect("modern", mode="legacy")
    with pytest.raises(ValueError):
        era_connect("modern", mode="newest")


def test_era_is_cached_per_server():
    era.forget()
    cmd = fixture_cmd("era_server.py", "legacy-silent")
    t, first = era.connect(lambda: StdioTransport(cmd), CLIENT, timeout=10, discover_timeout=1.5)
    t.close()
    assert not first.from_cache and first.fallback
    t, second = era.connect(lambda: StdioTransport(cmd), CLIENT, timeout=10, discover_timeout=1.5)
    t.close()
    # straight to initialize: no 1.5 s discover timeout the second time
    assert second.from_cache and second.era == "legacy" and second.fallback is None
    assert era.cached_era(StdioTransport(cmd).cache_key()) == "legacy"
    era.forget()
    assert era.cached_era(StdioTransport(cmd).cache_key()) is None


def test_modern_requests_carry_meta():
    t, hs = era_connect("modern")
    sent = []
    real = t._send
    t._send = lambda msg: (sent.append(msg), real(msg))[1]
    try:
        t.request("tools/list", {})
    finally:
        t.close()
    meta = sent[0]["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert meta["io.modelcontextprotocol/clientInfo"] == CLIENT
    assert meta["io.modelcontextprotocol/clientCapabilities"] == {}


def test_header_value_sentinel():
    assert header_value("add") == "add"
    assert header_value("notes/search.v2") == "notes/search.v2"
    assert header_value("héllo") == "=?base64?aMOpbGxv?="
    assert header_value(" padded") == "=?base64?IHBhZGRlZA==?="
    assert header_value("tab\there").startswith("=?base64?")
    assert header_value("=?base64?abc?=").startswith("=?base64?PT9iYXNl")


def test_label_from_supported_versions():
    hs = era.Handshake(era="modern", supported_versions=["2026-07-28", "2025-11-25"])
    assert hs.label() == "dual"
    assert era.Handshake(era="modern", supported_versions=["2026-07-28"]).label() == "modern"
    assert era.Handshake(era="legacy").label() == "legacy"
    assert era.is_modern_version("2026-07-28") and era.is_modern_version("2027-01-01")
    assert not era.is_modern_version("2025-11-25") and not era.is_modern_version(None)


# probe checks ------------------------------------------------------------------------

def test_probe_modern_only_passes():
    r = run("modern")
    assert (r.era, r.protocol_version, r.supported_versions) == ("modern", "2026-07-28", ["2026-07-28"])
    assert checks(r) == []
    assert {t.name: t.status for t in r.tools} == {"add": "pass"}


def test_probe_dual_era():
    r = run("dual")
    assert r.era == "dual"
    assert r.supported_versions == ["2026-07-28", "2025-11-25"]
    assert checks(r) == []


def test_probe_legacy_silent_warns():
    r = run("legacy-silent", discover_timeout=1.5)
    assert r.era == "legacy" and r.protocol_version == "2025-11-25"
    assert checks(r) == ["discover-unanswered"]
    assert "no answer to server/discover" in r.era_note
    assert not r.failed()


def test_probe_legacy_crash_warns_without_restart_count():
    r = run("legacy-crash")
    assert r.era == "legacy"
    assert checks(r) == ["discover-unanswered"]
    assert r.restarts == 0


def test_probe_missing_discover():
    r = run("modern-nodiscover")
    assert r.era == "modern"
    assert "discover-missing" in checks(r)
    assert r.failed()


def test_probe_missing_result_type_and_cache_fields():
    r = run("modern-nocache")
    found = checks(r)
    assert found.count("cache-fields-missing") == 2  # server/discover and tools/list
    assert "server-info-missing" in found
    assert found.count("result-type-missing") == 3  # discover, tools/list, tools/call
    tool_checks = [f.check for f in r.tools[0].findings]
    assert tool_checks == ["result-type-missing"]
    msg = next(f.message for f in r.findings if f.check == "cache-fields-missing" and "tools/list" in f.message)
    assert "no ttlMs" in msg and "no cacheScope" in msg


def test_probe_bad_discover_result():
    r = run("modern-baddiscover")
    assert r.era == "modern"
    assert "discover-invalid" in checks(r) and r.failed()


def test_probe_lenient_legacy_passes():
    r = run("legacy-lenient")
    assert r.era == "legacy" and checks(r) == []


def test_probe_wrong_error_codes():
    r = run("modern-badcodes")
    msgs = [f.message for f in r.findings if f.check == "error-code"]
    assert len(msgs) == 2
    assert "error code -32602, expected -32022" in msgs[0]
    assert "error code -32600, expected -32601" in msgs[1]
    assert "legacy-rejected-unhelpful" in checks(r)
    assert r.era == "modern"


def test_probe_input_required():
    r = run("modern-mrtr")
    by_tool = {t.name: [f.check for f in t.findings] for t in r.tools}
    assert by_tool == {"add": [], "ask": ["input-required"]}
    assert {t.name: t.status for t in r.tools} == {"add": "pass", "ask": "warn"}


def test_probe_forced_legacy_on_dual_server():
    r = run("dual", era="legacy")
    assert r.era == "legacy" and checks(r) == []


def test_probe_can_skip_era_checks():
    r = run("modern-badcodes", era_checks=False)
    assert "error-code" not in checks(r) and "legacy-rejected-unhelpful" not in checks(r)


def test_reports_show_era():
    r = run("dual")
    text = render_text([r])
    assert "protocol 2026-07-28, era dual (2026-07-28, 2025-11-25)" in text
    md = render_markdown([r])
    assert "era dual (2026-07-28, 2025-11-25)" in md
    doc = json.loads(render_json([r]))
    server = doc["servers"][0]
    assert server["era"] == "dual" and server["supported_versions"] == ["2026-07-28", "2025-11-25"]


def test_cli_era_flag(capsys):
    cmd = fixture_cmd("era_server.py", "dual")
    code = main(["--era", "legacy", "--format", "json", "--"] + cmd)
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["servers"][0]["era"] == "legacy"


# HTTP -------------------------------------------------------------------------------

@pytest.fixture(params=["modern", "modern-sse"])
def modern_http(request):
    srv, state, url = http_mcp.start(request.param)
    yield state, url
    srv.shutdown()
    srv.server_close()


def http_run(url, headers=None, **opts):
    opts.setdefault("timeout", 5)
    return Prober("h", lambda: HttpTransport(url, headers), Options(**opts), "http").run()


def test_http_modern_server(modern_http):
    state, url = modern_http
    r = http_run(url, {"Authorization": "Bearer t"})
    assert r.era == "modern" and r.protocol_version == "2026-07-28"
    assert checks(r) == []
    assert {t.name: t.status for t in r.tools} == {"add": "pass", "whoami": "pass"}
    # every modern request names its method; tools/call names its tool; no session id
    methods = [h[0] for h in state.modern_headers]
    assert methods[0] == "server/discover"
    assert ("tools/call", "add", "2026-07-28", None) in state.modern_headers
    assert all(h[3] is None for h in state.modern_headers)
    assert state.deleted == []
    assert state.calls == [("add", "2026-07-28"), ("whoami", "2026-07-28")]


def test_http_wrong_status_codes():
    srv, state, url = http_mcp.start("modern-badstatus")
    try:
        r = http_run(url)
    finally:
        srv.shutdown()
        srv.server_close()
    msgs = [f.message for f in r.findings if f.check == "error-code"]
    assert any("HTTP 200, expected 400" in m for m in msgs)
    assert any("HTTP 200, expected 404" in m for m in msgs)


def test_http_legacy_server_falls_back():
    srv, state, url = http_mcp.start("json")
    try:
        r = http_run(url)
    finally:
        srv.shutdown()
        srv.server_close()
    assert r.era == "legacy" and checks(r) == []
    assert "HTTP 400" in r.era_note
    assert state.calls == [("add", "2025-11-25"), ("whoami", "2025-11-25")]

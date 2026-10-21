"""Streamable HTTP transport against an in-process server."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
import http_mcp  # noqa: E402

from amagi_mcp_probe.probe import Options, Prober  # noqa: E402
from amagi_mcp_probe.transport import HttpTransport  # noqa: E402


@pytest.fixture(params=["json", "sse"])
def server(request):
    srv, state, url = http_mcp.start(request.param)
    yield state, url
    srv.shutdown()
    srv.server_close()


def run(url, headers=None, **opts):
    opts.setdefault("timeout", 5)
    return Prober("h", lambda: HttpTransport(url, headers), Options(**opts), "http").run()


def test_http_server_passes(server):
    state, url = server
    r = run(url, {"Authorization": "Bearer test-token"})
    assert r.findings == []
    assert {t.name: t.status for t in r.tools} == {"add": "pass", "whoami": "pass"}
    assert r.server_info["name"] == "http-fixture"
    # session header was accepted (otherwise the server answers 400), and closed with DELETE
    assert len(state.deleted) == 1 and state.deleted[0] in state.sessions
    # the negotiated protocol version is sent on later requests
    assert state.calls == [("add", "2025-11-25"), ("whoami", "2025-11-25")]


def test_custom_header_reaches_server(server):
    state, url = server
    t = HttpTransport(url, {"Authorization": "Bearer abc"})
    t.start()
    t.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "0"}})
    out = t.request("tools/call", {"name": "whoami", "arguments": {}})
    assert out["content"][0]["text"] == "Bearer abc"
    t.close()


def test_unreachable_url():
    r = run("http://127.0.0.1:9/mcp", startup_timeout=3)
    assert {f.check for f in r.findings} == {"initialize-error"}
    assert "could not reach" in r.findings[0].message


def test_not_a_url():
    r = run("localhost:3001/mcp")
    assert {f.check for f in r.findings} == {"server-start"}

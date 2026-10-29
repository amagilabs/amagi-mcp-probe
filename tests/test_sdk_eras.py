"""Era detection against the official MCP Python SDKs (mcp 1.x FastMCP, mcp 2.x MCPServer).

These run only when the scratch venvs exist: `python tests/sdk_envs.py` creates them.
"""

import os
import socket
import subprocess
import time

import pytest
import sdk_envs
from conftest import FIXTURES

from amagi_mcp_probe.probe import Options, Prober
from amagi_mcp_probe.transport import HttpTransport, StdioTransport

need_mcp1 = pytest.mark.skipif(not sdk_envs.available("mcp1"), reason="run tests/sdk_envs.py for the mcp 1.x venv")
need_mcp2 = pytest.mark.skipif(not sdk_envs.available("mcp2"), reason="run tests/sdk_envs.py for the mcp 2.x venv")


def sdk_cmd(env, fixture, *args):
    return [sdk_envs.python(env), os.path.join(FIXTURES, fixture)] + list(args)


def probe(name, factory, kind, **opts):
    opts.setdefault("timeout", 30)
    opts.setdefault("startup_timeout", 90)
    return Prober(name, factory, Options(**opts), kind).run()


def stdio(env, fixture, **opts):
    cmd = sdk_cmd(env, fixture)
    return probe(fixture, lambda: StdioTransport(cmd), "stdio", **opts)


def checks(result):
    return sorted(f.check for f in result.all_findings())


@need_mcp1
def test_sdk_legacy_stdio():
    r = stdio("mcp1", "sdk_legacy_server.py")
    assert r.era == "legacy" and r.protocol_version == "2025-11-25"
    assert r.server_info.get("name") == "sdk-legacy"
    assert not r.failed(), checks(r)
    assert {t.name: t.status for t in r.tools} == {"add": "pass", "echo": "pass"}


@need_mcp2
def test_sdk_dual_stdio():
    r = stdio("mcp2", "sdk_dual_server.py")
    assert r.era == "dual" and r.protocol_version == "2026-07-28"
    assert r.supported_versions == ["2026-07-28", "2025-11-25"]
    assert not r.failed(), checks(r)
    assert {t.name: t.status for t in r.tools} == {"add": "pass", "echo": "pass"}


@need_mcp2
def test_sdk_modern_stdio():
    r = stdio("mcp2", "sdk_modern_server.py")
    assert r.era == "modern" and r.supported_versions == ["2026-07-28"]
    assert not r.failed(), checks(r)
    assert {t.name: t.status for t in r.tools} == {"add": "pass", "echo": "pass"}


@need_mcp2
def test_sdk_dual_forced_legacy():
    r = stdio("mcp2", "sdk_dual_server.py", era="legacy")
    assert r.era == "legacy" and r.protocol_version == "2025-11-25"
    assert not r.failed(), checks(r)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(request):
    env, fixture = request.param
    if not sdk_envs.available(env):
        pytest.skip(f"run tests/sdk_envs.py for the {env} venv")
    port = free_port()
    proc = subprocess.Popen(sdk_cmd(env, fixture, "--http", str(port)),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 90  # a busy machine imports the SDK slowly
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            if proc.poll() is not None:
                pytest.fail(f"{fixture} exited with {proc.returncode}")
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail(f"{fixture} did not listen on {port}")
    yield fixture, f"http://127.0.0.1:{port}/mcp"
    proc.kill()
    proc.wait(10)


@pytest.mark.parametrize("http_server", [("mcp1", "sdk_legacy_server.py"), ("mcp2", "sdk_dual_server.py")],
                         indirect=True, ids=["legacy", "dual"])
def test_sdk_http(http_server):
    fixture, url = http_server
    r = probe(fixture, lambda: HttpTransport(url), "http")
    if fixture == "sdk_legacy_server.py":
        assert r.era == "legacy" and r.era_note
    else:
        assert r.era == "dual" and r.protocol_version == "2026-07-28"
    assert not r.failed(), checks(r)
    assert {t.name: t.status for t in r.tools} == {"add": "pass", "echo": "pass"}

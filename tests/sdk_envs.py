"""Scratch venvs with the official MCP Python SDKs, for tests/test_sdk_eras.py.

    python tests/sdk_envs.py          # create tests/.sdk-envs/mcp1 (mcp 1.x) and mcp2 (mcp 2.x)

Needs uv (https://docs.astral.sh/uv/) and network access. The SDK tests skip when the
venvs are missing, so plain `pytest` works without them.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, ".sdk-envs")
ENVS = {
    "mcp1": "mcp>=1.27,<2",  # FastMCP: legacy era only
    "mcp2": "mcp>=2.3,<3",  # MCPServer: 2026-07-28 and the legacy handshake
}


def python(name: str) -> str:
    sub = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    return os.path.join(ROOT, name, sub, exe)


def available(name: str) -> bool:
    return os.path.isfile(python(name))


def create() -> int:
    uv = shutil.which("uv")
    if uv is None:
        print("uv not found: install it from https://docs.astral.sh/uv/", file=sys.stderr)
        return 1
    os.makedirs(ROOT, exist_ok=True)
    for name, spec in ENVS.items():
        if available(name):
            print(f"{name}: exists")
            continue
        path = os.path.join(ROOT, name)
        subprocess.run([uv, "venv", "-q", "-p", "3.12", path], check=True)
        subprocess.run([uv, "pip", "install", "-q", "-p", python(name), spec], check=True)
        print(f"{name}: installed {spec}")
    return 0


if __name__ == "__main__":
    sys.exit(create())

"""Legacy-era fixture: an mcp 1.x FastMCP server over stdio (or Streamable HTTP with --http PORT).

Runs only in the tests/.sdk-envs/mcp1 venv (see tests/sdk_envs.py).
"""

import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sdk-legacy")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return text


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.settings.port = int(sys.argv[sys.argv.index("--http") + 1])
        mcp.settings.log_level = "WARNING"
        mcp.run(transport="streamable-http")
    else:
        mcp.run()

"""Dual-era fixture: an mcp 2.x MCPServer, which serves 2026-07-28 and the legacy handshake.

stdio by default, Streamable HTTP with --http PORT. Runs only in the tests/.sdk-envs/mcp2 venv.
"""

import sys

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("sdk-dual", version="2.0.0", instructions="Adds and echoes.")


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
        port = int(sys.argv[sys.argv.index("--http") + 1])
        mcp.run(transport="streamable-http", port=port, json_response=True, stateless_http=True)
    else:
        mcp.run()

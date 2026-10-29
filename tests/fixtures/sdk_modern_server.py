"""Modern-only fixture: an mcp 2.x MCPServer served on the 2026-07-28 loop alone over stdio.

`initialize` is answered with -32022 naming the supported versions, as the SDK does for a
modern-locked connection. Runs only in the tests/.sdk-envs/mcp2 venv.
"""

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.runner import _serve_modern_stream  # the SDK's modern-only serving loop
from mcp.server.stdio import stdio_server

mcp = MCPServer("sdk-modern", version="2.0.0")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return text


async def main():
    server = mcp._lowlevel_server
    async with stdio_server() as (read_stream, write_stream):
        async with server.lifespan(server) as state:
            await _serve_modern_stream(server, read_stream, write_stream, lifespan_state=state,
                                       raise_exceptions=False)


if __name__ == "__main__":
    anyio.run(main)

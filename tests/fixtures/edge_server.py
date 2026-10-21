"""Stdio MCP test server with odd behaviour, picked by argv[1].

exit     exits before answering anything
silent   reads stdin but never answers initialize
paged    two pages of tools with definition and result problems
loop     tools/list pagination that never ends
"""

import json
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "paged"


def write(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


OBJ = {"type": "object", "properties": {}}

PAGE1 = [
    {"name": "no_content", "description": "Returns a result with no content.", "inputSchema": OBJ},
    {"name": "odd_blocks", "description": "Returns odd content blocks.", "inputSchema": OBJ},
    {"name": "bad name!", "description": "Name breaks the naming rule.", "inputSchema": OBJ},
]
PAGE2 = [
    {"name": "no_content", "description": "Same name again.", "inputSchema": OBJ},
    {"name": "string_schema", "description": "inputSchema is not an object schema.",
     "inputSchema": {"type": "string"}},
    {"name": "ghost_required", "description": "Requires a property it never defines.",
     "inputSchema": {"type": "object", "properties": {}, "required": ["ghost"]}},
    {"name": "missing_structured", "description": "Declares outputSchema, returns no structuredContent.",
     "inputSchema": OBJ, "outputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
    {"name": "needs_task", "description": "Task-only tool.", "inputSchema": OBJ,
     "execution": {"taskSupport": "required"}},
    {"name": "fails_politely", "description": "Always reports isError.", "inputSchema": OBJ},
]

RESULTS = {
    "no_content": {"isError": "no"},
    "odd_blocks": {"content": [{"type": "text"}, {"type": "image", "data": "AAAA"}, {"type": "hologram"},
                               {"type": "resource", "resource": {"uri": "file:///x"}}],
                   "structuredContent": [1, 2]},
    "bad name!": {"content": [{"type": "text", "text": "ok"}]},
    "ghost_required": {"content": [{"type": "text", "text": "ok"}]},
    "missing_structured": {"content": [{"type": "text", "text": "ok"}]},
    "fails_politely": {"content": [{"type": "text", "text": "database is offline"}], "isError": True},
}


def main():
    if MODE == "exit":
        sys.stderr.write("fatal: API_KEY is not set\n")
        sys.stderr.flush()
        sys.exit(3)
    for line in sys.stdin:
        msg = json.loads(line)
        rid, method = msg.get("id"), msg.get("method")
        if rid is None or method is None:
            continue
        params = msg.get("params") or {}
        if MODE == "silent":
            continue
        if method == "initialize":
            write({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                "serverInfo": {"name": "edge", "version": "0.0.1"}}})
        elif method == "ping":
            write({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "tools/list":
            cursor = params.get("cursor")
            if MODE == "loop":
                write({"jsonrpc": "2.0", "id": rid, "result": {"tools": PAGE1[:1], "nextCursor": "again"}})
            elif cursor is None:
                write({"jsonrpc": "2.0", "id": rid, "result": {"tools": PAGE1, "nextCursor": "p2"}})
            else:
                write({"jsonrpc": "2.0", "id": rid, "result": {"tools": PAGE2}})
        elif method == "tools/call":
            write({"jsonrpc": "2.0", "id": rid, "result": RESULTS[params["name"]]})
        else:
            write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "nope"}})


if __name__ == "__main__":
    main()

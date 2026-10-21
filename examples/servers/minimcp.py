"""A tiny stdio MCP server loop (stdlib only) used by the example servers.

It handles initialize, ping, tools/list and tools/call. A handler returns a CallToolResult dict,
raises ToolError for an isError result, or raises InvalidParams for a JSON-RPC -32602 error.
Any other exception is not caught, so the process dies the way a careless real server would.
"""

import json
import sys

SUPPORTED = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]


class ToolError(Exception):
    pass


class InvalidParams(Exception):
    pass


def text(value, structured=None):
    result = {"content": [{"type": "text", "text": str(value)}]}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def _write(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def serve(name, version, tools):
    by_name = {t["name"]: t for t in tools}
    listed = [{k: v for k, v in t.items() if k != "handler"} for t in tools]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        rid = msg.get("id")
        method = msg.get("method")
        if rid is None or method is None:
            continue  # a notification, or a response to us
        params = msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            _write({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": asked if asked in SUPPORTED else SUPPORTED[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": name, "version": version},
            }})
        elif method == "ping":
            _write({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "tools/list":
            _write({"jsonrpc": "2.0", "id": rid, "result": {"tools": listed}})
        elif method == "tools/call":
            tool = by_name.get(params.get("name"))
            if tool is None:
                _write({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}})
                continue
            try:
                result = tool["handler"](params.get("arguments") or {})
            except ToolError as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            except InvalidParams as exc:
                _write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": str(exc)}})
                continue
            _write({"jsonrpc": "2.0", "id": rid, "result": result})
        else:
            _write({"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}})

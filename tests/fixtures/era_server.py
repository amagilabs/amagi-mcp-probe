"""A stdio MCP server for era tests (stdlib only). Usage: era_server.py MODE

modern             modern-only: server/discover, per-request _meta, -32022 for initialize
dual               modern, and answers initialize like a legacy server
legacy-silent      legacy; never answers server/discover
legacy-crash       legacy; exits when it gets server/discover
legacy-lenient     legacy; answers {} to any method it does not know, server/discover included
modern-nodiscover  modern requests work, but server/discover and initialize are unknown methods
modern-nocache     modern; no ttlMs/cacheScope/resultType, no serverInfo
modern-badcodes    modern; wrong error codes, and initialize rejected without naming versions
modern-mrtr        modern; the `ask` tool answers resultType input_required
modern-baddiscover modern; the server/discover result has no supportedVersions
slow-start         ignores the first server/discover, then -32022 to initialize, then modern
"""

import json
import sys

MODERN = ["2026-07-28"]
LEGACY = ["2025-11-25", "2025-06-18"]
META_VERSION = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO = {"io.modelcontextprotocol/serverInfo": {"name": "era-fixture", "version": "1.0"}}
TOOLS = [
    {"name": "add", "description": "Add two integers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                     "required": ["a", "b"]}},
]
ASK = {"name": "ask", "description": "Asks the user something.", "inputSchema": {"type": "object"}}

mode = sys.argv[1]
modern_ok = mode not in ("legacy-silent", "legacy-crash", "legacy-lenient")
legacy_ok = mode in ("dual", "legacy-silent", "legacy-crash", "legacy-lenient")
discover_seen = 0


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def error(rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    send({"jsonrpc": "2.0", "id": rid, "error": err})


def result(rid, res, cacheable=False):
    if mode != "modern-nocache":
        res = dict(res, resultType=res.get("resultType", "complete"), _meta=SERVER_INFO)
        if cacheable:
            res.update(ttlMs=0, cacheScope="private")
    send({"jsonrpc": "2.0", "id": rid, "result": res})


def tools():
    return TOOLS + ([ASK] if mode == "modern-mrtr" else [])


def call(params):
    args = params.get("arguments") or {}
    if params.get("name") == "ask":
        return {"resultType": "input_required", "inputRequests": {"q": {"method": "elicitation/create",
                                                                        "params": {"message": "Name?"}}}}
    total = args.get("a", 0) + args.get("b", 0)
    return {"content": [{"type": "text", "text": str(total)}], "isError": False}


def handle_modern(rid, method, params, version):
    global discover_seen
    if version not in MODERN:
        if mode == "modern-badcodes":
            return error(rid, -32602, "bad version")
        return error(rid, -32022, "Unsupported protocol version", {"supported": MODERN, "requested": version})
    if method == "server/discover":
        if mode == "modern-nodiscover":
            return error(rid, -32601, "Method not found")
        discover_seen += 1
        if mode == "slow-start" and discover_seen == 1:
            return  # still starting up: never answers this one
        res = {"supportedVersions": MODERN, "capabilities": {"tools": {}}}
        if mode == "modern-baddiscover":
            del res["supportedVersions"]
        return result(rid, res, cacheable=True)
    if method == "tools/list":
        return result(rid, {"tools": tools()}, cacheable=True)
    if method == "tools/call":
        res = call(params)
        if mode == "modern-nocache":
            return send({"jsonrpc": "2.0", "id": rid, "result": res})
        return result(rid, res)
    if mode == "modern-badcodes":
        return error(rid, -32600, "nope")
    return error(rid, -32601, "Method not found")


initialized = False
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    rid, method = msg.get("id"), msg.get("method")
    params = msg.get("params") or {}
    if rid is None:
        continue  # notifications
    meta = params.get("_meta") or {}
    if META_VERSION in meta:
        if not modern_ok:
            if method == "server/discover" and mode == "legacy-silent":
                continue
            if method == "server/discover" and mode == "legacy-crash":
                sys.exit(3)
            if mode == "legacy-lenient":
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
                continue
            error(rid, -32601, "Method not found")
            continue
        handle_modern(rid, method, params, meta[META_VERSION])
        continue
    # legacy-shaped request
    if method == "initialize":
        if legacy_ok:
            initialized = True
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": params.get("protocolVersion") if params.get("protocolVersion") in LEGACY else LEGACY[0],
                "capabilities": {"tools": {}}, "serverInfo": {"name": "era-fixture", "version": "1.0"}}})
        elif mode in ("modern-badcodes", "modern-nodiscover"):
            error(rid, -32601, "Method not found")
        else:
            error(rid, -32022, "Unsupported protocol version",
                  {"supported": MODERN, "requested": params.get("protocolVersion")})
    elif not legacy_ok:
        error(rid, -32022, "Unsupported protocol version", {"supported": MODERN})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": rid, "result": call(params)})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    else:
        error(rid, -32601, "Method not found")

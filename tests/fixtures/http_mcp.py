"""An in-process Streamable HTTP MCP server for tests (stdlib only)."""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "add", "description": "Add two integers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                     "required": ["a", "b"]},
     "outputSchema": {"type": "object", "properties": {"sum": {"type": "integer"}}, "required": ["sum"]}},
    {"name": "whoami", "description": "Echo the auth header back.", "inputSchema": {"type": "object"}},
]


class State:
    def __init__(self, mode):
        self.mode = mode  # "json" or "sse"
        self.sessions = set()
        self.deleted = []
        self.calls = []


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_DELETE(self):
            state.deleted.append(self.headers.get("Mcp-Session-Id"))
            self._send(200, "")

        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method, rid = msg.get("method"), msg.get("id")
            extra = {}
            if method == "initialize":
                sid = uuid.uuid4().hex
                state.sessions.add(sid)
                extra["Mcp-Session-Id"] = sid
                result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "http-fixture", "version": "1.0"}}
            else:
                if self.headers.get("Mcp-Session-Id") not in state.sessions:
                    body = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "bad session"}}
                    return self._send(400, json.dumps(body))
                if rid is None:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "ping":
                    result = {}
                elif method == "tools/call":
                    state.calls.append((msg["params"]["name"], self.headers.get("MCP-Protocol-Version")))
                    args = msg["params"].get("arguments") or {}
                    if msg["params"]["name"] == "add":
                        total = args["a"] + args["b"]
                        result = {"content": [{"type": "text", "text": str(total)}],
                                  "structuredContent": {"sum": total}}
                    else:
                        auth = self.headers.get("Authorization", "none")
                        result = {"content": [{"type": "text", "text": auth}]}
                else:
                    result = None
            reply = {"jsonrpc": "2.0", "id": rid, "result": result}
            if state.mode == "sse":
                note = json.dumps({"jsonrpc": "2.0", "method": "notifications/message",
                                   "params": {"level": "info", "data": "working"}})
                # priming event (id + empty data), a comment, a notification, then the response
                body = (f"id: 0\ndata: \n\n: keepalive\n\nevent: message\ndata: {note}\n\n"
                        f"id: 1\ndata: {json.dumps(reply)}\n\n")
                self._send(200, body, "text/event-stream", extra)
            else:
                self._send(200, json.dumps(reply), "application/json", extra)

    return Handler


def start(mode="json"):
    state = State(mode)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state, f"http://127.0.0.1:{server.server_address[1]}/mcp"

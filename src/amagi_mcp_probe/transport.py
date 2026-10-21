"""JSON-RPC 2.0 transports for talking to an MCP server: stdio and Streamable HTTP."""

from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Dict, List, Optional

_EOF = object()


class TransportError(Exception):
    """The server could not be reached, started, or talked to."""


class ServerExited(TransportError):
    """The server process exited (or closed stdout) while we were waiting on it."""

    def __init__(self, returncode: Optional[int]):
        self.returncode = returncode
        code = "unknown code" if returncode is None else f"code {returncode}"
        super().__init__(f"server process exited ({code})")


class RequestTimeout(TransportError):
    """No response arrived within the timeout."""


class RpcError(Exception):
    """The server answered with a JSON-RPC error object."""

    def __init__(self, code: Any, message: Any, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"JSON-RPC error {code}: {message}")


class Transport:
    """Common request/notify plumbing. Subclasses implement start/_send/_next/close."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.protocol_version: Optional[str] = None
        self.noise: List[str] = []
        self.notifications = 0

    # subclass hooks -----------------------------------------------------
    def start(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError

    def stderr_tail(self, lines: int = 20) -> List[str]:
        return []

    def alive(self) -> bool:
        return True

    # shared -------------------------------------------------------------
    def _handle_server_request(self, msg: Dict[str, Any]) -> None:
        """Answer a request the server sent us. We only implement ping."""
        if msg.get("method") == "ping":
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        else:
            self._send({
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32601, "message": "Method not found (amagi-mcp-probe declares no client capabilities)"},
            })

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Any:
        rid = next(self._ids)
        self.last_request_id = rid
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        return self._roundtrip(msg, rid, timeout)

    def _roundtrip(self, msg: Dict[str, Any], rid: int, timeout: float) -> Any:
        raise NotImplementedError

    def _match(self, msg: Any, rid: int) -> Optional[tuple]:
        """Classify one incoming message. Returns ("result", value) for our response."""
        if not isinstance(msg, dict):
            return None
        if "method" in msg:
            if "id" in msg:
                self._handle_server_request(msg)
            else:
                self.notifications += 1
            return None
        if msg.get("id") != rid:
            return None  # late answer to a request we already gave up on
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise RpcError(err.get("code"), err.get("message", msg["error"]), err.get("data"))
        if "result" not in msg:
            raise RpcError(None, "response has neither 'result' nor 'error'")
        return ("result", msg["result"])


def resolve_command(command: List[str]) -> List[str]:
    """Find the executable on PATH the way a shell would (PATHEXT on Windows, so `npx` works)."""
    if not command:
        raise TransportError("no server command given")
    exe = command[0]
    found = shutil.which(exe)
    if found is None:
        if os.path.isfile(exe):
            found = exe
        else:
            raise TransportError(f"command not found: {exe}")
    return [found] + list(command[1:])


class StdioTransport(Transport):
    """Launch the server as a child process and speak newline-delimited JSON-RPC on its stdio."""

    def __init__(self, command: List[str], env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None):
        super().__init__()
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self._inbox: "queue.Queue[Any]" = queue.Queue()
        self._stderr: deque = deque(maxlen=200)
        self._lock = threading.Lock()

    def describe(self) -> str:
        return " ".join(self.command)

    def start(self) -> None:
        argv = resolve_command(self.command)
        env = dict(os.environ)
        env.update(self.env)
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self.cwd,
                bufsize=0,
                **kwargs,
            )
        except OSError as exc:
            raise TransportError(f"could not start server: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    if len(self.noise) < 20:
                        self.noise.append(line[:300])
                    continue
                if isinstance(msg, list):  # JSON-RPC batch
                    for item in msg:
                        self._inbox.put(item)
                elif isinstance(msg, dict):
                    self._inbox.put(msg)
                elif len(self.noise) < 20:
                    self.noise.append(line[:300])
        except (OSError, ValueError):
            pass
        finally:
            self._inbox.put(_EOF)

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                self._stderr.append(raw.decode("utf-8", errors="replace").rstrip())
        except (OSError, ValueError):
            pass

    def stderr_tail(self, lines: int = 20) -> List[str]:
        return list(self._stderr)[-lines:]

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _send(self, msg: Dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise TransportError("server not started")
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ServerExited(self._exit_code()) from exc

    def _exit_code(self, wait: float = 2.0) -> Optional[int]:
        if self.proc is None:
            return None
        try:
            return self.proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            return None

    def _roundtrip(self, msg: Dict[str, Any], rid: int, timeout: float) -> Any:
        self._send(msg)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RequestTimeout(f"no response to {msg.get('method')} within {timeout:g}s")
            try:
                item = self._inbox.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue  # if the process died, the stdout reader posts _EOF
            if item is _EOF:
                self._inbox.put(_EOF)  # keep EOF visible to later calls
                raise ServerExited(self._exit_code())
            hit = self._match(item, rid)
            if hit:
                return hit[1]

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        _kill_tree(proc)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop the server and anything it spawned (npx -> node, uv -> python, ...)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                break
            try:
                proc.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class HttpTransport(Transport):
    """MCP Streamable HTTP: POST each message, read a JSON or text/event-stream reply."""

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None):
        super().__init__()
        self.url = url
        self.headers = dict(headers or {})
        self.session_id: Optional[str] = None
        self._pending: deque = deque()

    def describe(self) -> str:
        return self.url

    def start(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise TransportError(f"not an http(s) URL: {self.url}")

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "amagi-mcp-probe",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        h.update(self.headers)
        return h

    def _post(self, msg: Dict[str, Any], timeout: float):
        body = json.dumps(msg).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=self._headers(), method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                if isinstance(parsed, dict) and "error" in parsed:
                    return [parsed]
            except ValueError:
                pass
            raise TransportError(f"HTTP {exc.code} from server: {detail.strip()[:200]}") from exc
        except (TimeoutError, OSError) as exc:
            if "timed out" in str(exc).lower():
                raise RequestTimeout(f"no response to {msg.get('method')} within {timeout:g}s") from exc
            raise TransportError(f"could not reach {self.url}: {exc}") from exc
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        ctype = (resp.headers.get("Content-Type") or "").lower()
        return resp, ctype

    def _send(self, msg: Dict[str, Any]) -> None:
        out = self._post(msg, timeout=10)
        if isinstance(out, list):
            return
        resp, _ = out
        resp.read()
        resp.close()

    def _roundtrip(self, msg: Dict[str, Any], rid: int, timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        out = self._post(msg, timeout)
        if isinstance(out, list):
            messages = out
            for m in messages:
                hit = self._match(m, rid)
                if hit:
                    return hit[1]
            raise TransportError("server answered with an error body but no matching response")
        resp, ctype = out
        try:
            if "text/event-stream" in ctype:
                for data in _sse_events(resp, deadline):
                    if not data.strip():
                        continue  # a priming event (id, empty data) lets the client resume; not a message
                    try:
                        m = json.loads(data)
                    except ValueError:
                        if len(self.noise) < 20:
                            self.noise.append(data[:300])
                        continue
                    for item in (m if isinstance(m, list) else [m]):
                        hit = self._match(item, rid)
                        if hit:
                            return hit[1]
                raise TransportError(f"event stream for {msg.get('method')} closed without a response")
            raw = resp.read()
        except (TimeoutError, OSError) as exc:
            raise RequestTimeout(f"no response to {msg.get('method')} within {timeout:g}s") from exc
        finally:
            resp.close()
        try:
            m = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            self.noise.append(raw[:300].decode("utf-8", errors="replace"))
            raise TransportError(f"response to {msg.get('method')} is not JSON (Content-Type: {ctype or 'none'})") from exc
        for item in (m if isinstance(m, list) else [m]):
            hit = self._match(item, rid)
            if hit:
                return hit[1]
        raise TransportError(f"no response with id {rid} in the reply to {msg.get('method')}")

    def close(self) -> None:
        if not self.session_id:
            return
        req = urllib.request.Request(self.url, headers=self._headers(), method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except (urllib.error.URLError, OSError):
            pass


def _sse_events(resp, deadline: float):
    """Yield the data payload of each server-sent event."""
    data: List[str] = []
    for raw in iter(resp.readline, b""):
        if time.monotonic() > deadline:
            raise RequestTimeout("event stream did not deliver a response in time")
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
    if data:
        yield "\n".join(data)


__all__ = [
    "HttpTransport",
    "RequestTimeout",
    "RpcError",
    "ServerExited",
    "StdioTransport",
    "Transport",
    "TransportError",
    "resolve_command",
]

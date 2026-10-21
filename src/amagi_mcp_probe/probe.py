"""Run the smoke test against one MCP server."""

from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from . import __version__
from .model import CHECKS, ERROR, Finding, ServerResult, ToolResult
from .samples import Unsatisfiable, generate
from .schemas import make_validator, schema_problems
from .transport import RequestTimeout, RpcError, ServerExited, Transport, TransportError

LATEST_PROTOCOL = "2025-11-25"
KNOWN_PROTOCOLS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
CONTENT_TYPES = {"text", "image", "audio", "resource_link", "resource"}
MAX_PAGES = 100


@dataclass
class Options:
    timeout: float = 10.0
    startup_timeout: float = 30.0
    only: List[str] = field(default_factory=list)
    skip: List[str] = field(default_factory=list)
    call: bool = True
    allow_destructive: bool = False
    include_optional: bool = False
    args: Dict[str, Any] = field(default_factory=dict)
    ignore: Set[str] = field(default_factory=set)
    protocol_version: str = LATEST_PROTOCOL
    max_restarts: int = 3


class _Recorder:
    def __init__(self, ignore: Set[str]):
        self.ignore = ignore

    def add(self, bucket: List[Finding], check: str, message: str, tool: Optional[str] = None,
            detail: Optional[List[str]] = None) -> None:
        if check in self.ignore:
            return
        severity = CHECKS[check][0]
        bucket.append(Finding(check=check, severity=severity, message=message, tool=tool, detail=list(detail or [])))


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


class Prober:
    def __init__(self, name: str, factory: Callable[[], Transport], options: Options, transport_kind: str):
        self.name = name
        self.factory = factory
        self.opt = options
        self.rec = _Recorder(options.ignore)
        self.transport: Optional[Transport] = None
        self.result = ServerResult(name=name, target="", transport=transport_kind)
        self._noise_seen: List[str] = []
        self._noise_blamed: List[str] = []
        self._stderr_seen: List[str] = []

    # session ----------------------------------------------------------------
    def _connect(self, first: bool) -> bool:
        """Start the server and run the initialize handshake. Returns False on failure."""
        t = self.factory()
        self.transport = t
        self.result.target = t.describe()
        start = time.monotonic()
        bucket = self.result.findings
        try:
            t.start()
        except TransportError as exc:
            self.rec.add(bucket, "server-start", str(exc))
            return False
        params = {
            "protocolVersion": self.opt.protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "amagi-mcp-probe", "version": __version__},
        }
        try:
            init = t.request("initialize", params, timeout=self.opt.startup_timeout)
        except RequestTimeout:
            self.rec.add(bucket, "initialize-timeout",
                         f"no answer to initialize within {self.opt.startup_timeout:g}s", detail=self._stderr(t))
            return False
        except ServerExited as exc:
            self.rec.add(bucket, "initialize-error", f"{exc} during initialize", detail=self._stderr(t))
            return False
        except RpcError as exc:
            self.rec.add(bucket, "initialize-error", str(exc), detail=self._stderr(t))
            return False
        except TransportError as exc:
            self.rec.add(bucket, "initialize-error", str(exc))
            return False
        if first:
            self.result.startup_ms = _ms(start)
            self._check_initialize(init)
        version = init.get("protocolVersion") if isinstance(init, dict) else None
        t.protocol_version = version if isinstance(version, str) else self.opt.protocol_version
        try:
            t.notify("notifications/initialized")
        except TransportError as exc:
            self.rec.add(bucket, "initialize-error", f"could not send notifications/initialized: {exc}")
            return False
        return True

    def _check_initialize(self, init: Any) -> None:
        bucket = self.result.findings
        if not isinstance(init, dict):
            self.rec.add(bucket, "initialize-invalid", f"initialize result is {type(init).__name__}, not an object")
            return
        missing = [k for k in ("protocolVersion", "capabilities", "serverInfo") if k not in init]
        if missing:
            self.rec.add(bucket, "initialize-invalid", "initialize result is missing " + ", ".join(missing))
        version = init.get("protocolVersion")
        self.result.protocol_version = version if isinstance(version, str) else None
        if isinstance(version, str) and version not in KNOWN_PROTOCOLS:
            self.rec.add(bucket, "protocol-version",
                         f"server chose protocol {version!r}; this probe knows {', '.join(sorted(KNOWN_PROTOCOLS))}")
        info = init.get("serverInfo")
        if isinstance(info, dict):
            self.result.server_info = {k: info[k] for k in ("name", "title", "version") if k in info}
        elif "serverInfo" in init:
            self.rec.add(bucket, "initialize-invalid", "serverInfo is not an object")
        caps = init.get("capabilities")
        self._caps = caps if isinstance(caps, dict) else {}

    def _stderr(self, t: Transport) -> List[str]:
        """The last few stderr lines: for a Python traceback, that is the exception itself."""
        return [line for line in t.stderr_tail(40) if line.strip()][-8:]

    def _harvest(self) -> None:
        """Keep stdout noise and stderr from a session before it is replaced."""
        t = self.transport
        if t is None:
            return
        for line in t.noise:
            if line not in self._noise_seen:
                self._noise_seen.append(line)
        self._stderr_seen = t.stderr_tail(30) or self._stderr_seen

    def _close(self) -> None:
        if self.transport is not None:
            self._harvest()
            try:
                self.transport.close()
            except Exception:
                pass

    def _restart(self, reason_tool: str) -> bool:
        self._close()
        if self.result.restarts >= self.opt.max_restarts:
            self.rec.add(self.result.findings, "too-many-restarts",
                         f"server had to be restarted {self.result.restarts} times; stopped after {reason_tool}")
            return False
        self.result.restarts += 1
        return self._connect(first=False)

    # main ---------------------------------------------------------------------
    def run(self) -> ServerResult:
        self._caps: Dict[str, Any] = {}
        try:
            if not self._connect(first=True):
                return self.result
            tools = self._list_tools()
            if tools is None:
                return self.result
            if tools and "tools" not in self._caps:
                self.rec.add(self.result.findings, "no-tools-capability",
                             "tools/list returned tools, but initialize did not declare the `tools` capability")
            self._check_definitions(tools)
            self._call_all(tools)
        finally:
            self._close()
            loose = [line for line in self._noise_seen if line not in self._noise_blamed]
            if loose:
                self.rec.add(self.result.findings, "stdout-noise",
                             f"{len(loose)} line(s) on stdout were not JSON-RPC; log to stderr instead",
                             detail=loose[:5])
            self.result.stderr_tail = self._stderr_seen[-20:]
            for tr in self.result.tools:
                tr.finalize()
        return self.result

    def _list_tools(self) -> Optional[List[Dict[str, Any]]]:
        assert self.transport is not None
        bucket = self.result.findings
        tools: List[Dict[str, Any]] = []
        cursor = None
        seen_cursors = set()
        for _ in range(MAX_PAGES):
            params = {"cursor": cursor} if cursor else {}
            try:
                page = self.transport.request("tools/list", params, timeout=self.opt.timeout)
            except (TransportError, RpcError) as exc:
                self.rec.add(bucket, "tools-list-error", f"tools/list failed: {exc}",
                             detail=self._stderr(self.transport))
                return None
            if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
                self.rec.add(bucket, "tools-list-error", "tools/list result has no `tools` array")
                return None
            for item in page["tools"]:
                if isinstance(item, dict):
                    tools.append(item)
                else:
                    self.rec.add(bucket, "tools-list-error", f"tools/list entry is not an object: {item!r:.80}")
            cursor = page.get("nextCursor")
            if not cursor:
                return tools
            if cursor in seen_cursors:
                self.rec.add(bucket, "tools-list-error", f"tools/list pagination loops on cursor {cursor!r:.40}")
                return tools
            seen_cursors.add(cursor)
        self.rec.add(bucket, "tools-list-error", f"tools/list returned more than {MAX_PAGES} pages")
        return tools

    # definitions ----------------------------------------------------------------
    def _check_definitions(self, tools: List[Dict[str, Any]]) -> None:
        seen: Dict[str, int] = {}
        for i, tool in enumerate(tools):
            name = tool.get("name")
            label = name if isinstance(name, str) and name else f"<tool #{i + 1}>"
            tr = ToolResult(name=label)
            self.result.tools.append(tr)
            f = tr.findings
            if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
                self.rec.add(f, "tool-name", f"tool name {name!r} should be 1-128 chars of A-Z a-z 0-9 _ - .", label)
            if isinstance(name, str):
                seen[name] = seen.get(name, 0) + 1
                if seen[name] == 2:
                    self.rec.add(f, "tool-name-duplicate", f"more than one tool is named {name!r}", label)
            desc = tool.get("description")
            if not isinstance(desc, str) or not desc.strip():
                self.rec.add(f, "tool-description", "no description; models pick tools by their description", label)

            schema = tool.get("inputSchema")
            if schema is None:
                self.rec.add(f, "input-schema-missing", "inputSchema is missing", label)
            elif not isinstance(schema, dict):
                self.rec.add(f, "input-schema-type", f"inputSchema is {type(schema).__name__}, not an object", label)
            else:
                if schema.get("type") != "object":
                    self.rec.add(f, "input-schema-type",
                                 f"inputSchema type is {schema.get('type')!r}; MCP requires \"object\"", label)
                errors, notes = schema_problems(schema)
                for e in errors:
                    self.rec.add(f, "input-schema-invalid", e, label)
                for n in notes:
                    self.rec.add(f, "schema-note", f"inputSchema: {n}", label)
                props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
                req = schema.get("required") if isinstance(schema.get("required"), list) else []
                undefined = [r for r in req if isinstance(r, str) and r not in props]
                if undefined and not schema.get("patternProperties") and schema.get("additionalProperties") is not False:
                    self.rec.add(f, "required-not-defined",
                                 "required but not in properties: " + ", ".join(undefined), label)
                elif undefined and schema.get("additionalProperties") is False:
                    self.rec.add(f, "input-schema-invalid",
                                 "required but not in properties, and additionalProperties is false: "
                                 + ", ".join(undefined), label)

            out = tool.get("outputSchema")
            if out is not None:
                if not isinstance(out, dict) or out.get("type") != "object":
                    self.rec.add(f, "output-schema-invalid", "outputSchema must be an object schema with type \"object\"", label)
                else:
                    errors, notes = schema_problems(out)
                    for e in errors:
                        self.rec.add(f, "output-schema-invalid", e, label)
                    for n in notes:
                        self.rec.add(f, "schema-note", f"outputSchema: {n}", label)

    # calls ----------------------------------------------------------------------
    def _selected(self, name: str) -> Optional[str]:
        if self.opt.only and not any(fnmatch.fnmatchcase(name, p) for p in self.opt.only):
            return "not matched by --only"
        if any(fnmatch.fnmatchcase(name, p) for p in self.opt.skip):
            return "matched --skip"
        return None

    def _call_all(self, tools: List[Dict[str, Any]]) -> None:
        stopped = False
        for tool, tr in zip(tools, self.result.tools):
            if stopped:
                tr.status = "not-run"
                tr.skip_reason = "server could not be restarted"
                continue
            reason = self._skip_reason(tool, tr)
            if reason:
                tr.status = "skip"
                tr.skip_reason = reason
                continue
            ok = self._call_one(tool, tr)
            if not ok:
                stopped = True

    def _skip_reason(self, tool: Dict[str, Any], tr: ToolResult) -> Optional[str]:
        if not self.opt.call:
            return "--no-call"
        reason = self._selected(tr.name)
        if reason:
            return reason
        if not isinstance(tool.get("name"), str):
            return "tool has no usable name"
        ann = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        if ann.get("destructiveHint") is True and not self.opt.allow_destructive:
            return "destructiveHint: true (use --allow-destructive to call it)"
        execution = tool.get("execution") if isinstance(tool.get("execution"), dict) else {}
        if execution.get("taskSupport") == "required":
            return "needs task-based execution, which this probe does not run"
        if any(f.check in ("input-schema-missing", "input-schema-type", "input-schema-invalid") for f in tr.findings):
            return "inputSchema is unusable"
        return None

    def _arguments(self, tool: Dict[str, Any], tr: ToolResult) -> Optional[List[Any]]:
        name = tr.name
        schema = tool.get("inputSchema") or {"type": "object"}
        if name in self.opt.args:
            given = self.opt.args[name]
            calls = given if isinstance(given, list) else [given]
            tr.args_source = "--args"
            validator = make_validator(schema)
            for i, args in enumerate(calls):
                errors = [e.message for e in validator.iter_errors(args)]
                if errors:
                    which = f" (call {i + 1})" if len(calls) > 1 else ""
                    self.rec.add(tr.findings, "args-override-invalid",
                                 f"--args{which} does not match inputSchema: {errors[0]}", name)
            return calls
        try:
            args = generate(schema, include_optional=self.opt.include_optional)
        except Unsatisfiable as exc:
            self.rec.add(tr.findings, "args-unsatisfiable", str(exc), name)
            tr.status = "skip"
            tr.skip_reason = "no valid sample arguments"
            return None
        tr.args_source = "generated"
        return [args]

    def _call_one(self, tool: Dict[str, Any], tr: ToolResult) -> bool:
        """Call one tool. Returns False if the server is gone and could not be restarted."""
        calls = self._arguments(tool, tr)
        if calls is None:
            return True
        tr.arguments = calls[0] if len(calls) == 1 else calls
        total = 0
        for args in calls:
            assert self.transport is not None
            noise_before = len(self.transport.noise)
            start = time.monotonic()
            try:
                result = self.transport.request("tools/call", {"name": tr.name, "arguments": args},
                                                timeout=self.opt.timeout)
            except ServerExited as exc:
                total += _ms(start)
                self._harvest()
                self.rec.add(tr.findings, "server-crash", f"{exc} while running this tool", tr.name,
                             detail=self._stderr(self.transport))
                tr.duration_ms = total
                return self._restart(tr.name)
            except RequestTimeout:
                total += _ms(start)
                self.rec.add(tr.findings, "call-timeout", f"no answer within {self.opt.timeout:g}s", tr.name)
                tr.duration_ms = total
                return self._recover_after_timeout(tr.name)
            except RpcError as exc:
                total += _ms(start)
                hint = ""
                if exc.code == -32602:
                    hint = " (the arguments match the published inputSchema, so the schema and the handler disagree)"
                self.rec.add(tr.findings, "call-rpc-error", f"{exc}{hint}", tr.name)
                continue
            except TransportError as exc:
                total += _ms(start)
                self.rec.add(tr.findings, "call-rpc-error", str(exc), tr.name)
                continue
            total += _ms(start)
            self._check_result(tool, tr, result)
            self._blame_noise(tr, noise_before)
        tr.duration_ms = total
        return True

    def _blame_noise(self, tr: ToolResult, before: int) -> None:
        """Stdout noise that showed up while this tool ran is this tool's finding."""
        assert self.transport is not None
        new = self.transport.noise[before:]
        if new:
            self._noise_blamed.extend(new)
            self.rec.add(tr.findings, "stdout-noise",
                         f"wrote {len(new)} non-JSON-RPC line(s) to stdout while running; log to stderr instead",
                         tr.name, detail=new[:3])

    def _recover_after_timeout(self, name: str) -> bool:
        t = self.transport
        assert t is not None
        rid = getattr(t, "last_request_id", None)
        try:
            if rid is not None:
                t.notify("notifications/cancelled", {"requestId": rid, "reason": "amagi-mcp-probe timeout"})
            t.request("ping", {}, timeout=min(5.0, self.opt.timeout))
            return True
        except (TransportError, RpcError):
            return self._restart(name)

    def _check_result(self, tool: Dict[str, Any], tr: ToolResult, result: Any) -> None:
        name = tr.name
        f = tr.findings
        problems = result_problems(result)
        for p in problems:
            self.rec.add(f, "call-result-invalid", p, name)
        if not isinstance(result, dict):
            return
        content = result.get("content") if isinstance(result.get("content"), list) else []
        for block in content:
            if isinstance(block, dict) and block.get("type") not in CONTENT_TYPES and isinstance(block.get("type"), str):
                self.rec.add(f, "content-type-unknown", f"content block type {block.get('type')!r}", name)
        is_error = result.get("isError") is True
        if is_error:
            text = next((b.get("text") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)), "")
            first = (text or "").strip().splitlines()[0][:160] if (text or "").strip() else "(no text)"
            self.rec.add(f, "tool-error", f"isError: true: {first}", name)
        out = tool.get("outputSchema")
        if isinstance(out, dict) and not is_error and not any(x.check == "output-schema-invalid" for x in f):
            if "structuredContent" not in result:
                self.rec.add(f, "structured-content-missing",
                             "outputSchema is declared but the result has no structuredContent", name)
            else:
                try:
                    errors = list(make_validator(out).iter_errors(result["structuredContent"]))
                except Exception as exc:
                    errors = []
                    self.rec.add(f, "output-schema-invalid", f"outputSchema cannot validate: {exc}", name)
                if errors:
                    first = errors[0]
                    where = "/".join(str(p) for p in first.absolute_path) or "(root)"
                    self.rec.add(f, "structured-content-invalid",
                                 f"structuredContent {where}: {first.message}", name,
                                 detail=[f"{len(errors)} validation error(s)"] if len(errors) > 1 else None)


def result_problems(result: Any) -> List[str]:
    """Shape problems in a CallToolResult (MCP 2025-11-25)."""
    if not isinstance(result, dict):
        return [f"result is {type(result).__name__}, not an object"]
    out: List[str] = []
    content = result.get("content")
    if not isinstance(content, list):
        out.append("result has no `content` array")
        content = []
    for i, block in enumerate(content):
        where = f"content[{i}]"
        if not isinstance(block, dict):
            out.append(f"{where} is not an object")
            continue
        kind = block.get("type")
        if not isinstance(kind, str):
            out.append(f"{where} has no `type`")
        elif kind == "text":
            if not isinstance(block.get("text"), str):
                out.append(f"{where} type text needs a string `text`")
        elif kind in ("image", "audio"):
            for key in ("data", "mimeType"):
                if not isinstance(block.get(key), str):
                    out.append(f"{where} type {kind} needs a string `{key}`")
        elif kind == "resource_link":
            for key in ("uri", "name"):
                if not isinstance(block.get(key), str):
                    out.append(f"{where} type resource_link needs a string `{key}`")
        elif kind == "resource":
            res = block.get("resource")
            if not isinstance(res, dict) or not isinstance(res.get("uri"), str):
                out.append(f"{where} type resource needs `resource.uri`")
            elif not isinstance(res.get("text"), str) and not isinstance(res.get("blob"), str):
                out.append(f"{where} type resource needs `resource.text` or `resource.blob`")
    if "isError" in result and not isinstance(result["isError"], bool):
        out.append("`isError` is not a boolean")
    if "structuredContent" in result and not isinstance(result["structuredContent"], dict):
        out.append("`structuredContent` is not an object")
    return out

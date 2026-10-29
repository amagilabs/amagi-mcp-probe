"""Run the smoke test against one MCP server."""

from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from . import __version__
from .model import CHECKS, ERROR, Finding, ServerResult, ToolResult
from .samples import Unsatisfiable, generate
from .schemas import make_validator, schema_problems
from .era import (
    LATEST_LEGACY,
    LEGACY_VERSIONS,
    MODERN_VERSIONS,
    UNSUPPORTED_VERSION,
    ConnectError,
    Handshake,
    connect,
    supported_from,
)
from .transport import (
    META_SERVER_INFO,
    HttpStatusError,
    RequestTimeout,
    RpcError,
    ServerExited,
    Transport,
    TransportError,
)

LATEST_PROTOCOL = LATEST_LEGACY
KNOWN_PROTOCOLS = set(LEGACY_VERSIONS + MODERN_VERSIONS)
RESULT_TYPES = {"complete", "input_required"}
CACHE_SCOPES = {"public", "private"}
BOGUS_VERSION = "2099-01-01"  # a version no server supports, to check the -32022 answer
BOGUS_METHOD = "amagi-mcp-probe/no-such-method"
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
CONTENT_TYPES = {"text", "image", "audio", "resource_link", "resource"}
MAX_PAGES = 100


def _bases(t: Transport) -> List[str]:
    """Folders whose paths in stderr are printed relative: the server's cwd, then ours."""
    cwd = getattr(t, "cwd", None)
    return ([os.path.abspath(cwd)] if cwd else []) + [os.getcwd()]


def shorten_paths(lines: List[str], bases: List[str]) -> List[str]:
    """Print paths under the working directory as relative paths, so reports do not leak local folders."""
    patterns = []
    for base in bases:
        base = base.rstrip("\\/")
        if len(base) < 3:  # never strip a bare drive or root
            continue
        # match the folder with either slash style, followed by a separator
        parts = [re.escape(p) for p in re.split(r"[\\/]", base)]
        patterns.append(re.compile(r"[\\/]".join(parts) + r"[\\/]", re.IGNORECASE if os.name == "nt" else 0))
    out = []
    for line in lines:
        for pattern in patterns:
            line = pattern.sub("", line)
        out.append(line)
    return out


@dataclass
class Options:
    timeout: float = 30.0
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
    era: str = "auto"  # auto | modern | legacy
    discover_timeout: Optional[float] = None  # default: min(startup_timeout, 10s)
    era_checks: bool = True  # on modern servers: try initialize, a bad version, an unknown method


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
    def _make(self) -> Transport:
        t = self.factory()
        self.transport = t
        self.result.target = t.describe()
        return t

    def _connect(self, first: bool) -> bool:
        """Start the server and open a session (server/discover, or initialize). Returns False on failure."""
        start = time.monotonic()
        bucket = self.result.findings
        mode = self.opt.era if first else (self._era or self.opt.era)
        try:
            t, hs = connect(
                self._make,
                {"name": "amagi-mcp-probe", "version": __version__},
                mode=mode,
                legacy_version=self.opt.protocol_version,
                timeout=self.opt.startup_timeout,
                discover_timeout=self.opt.discover_timeout,
                use_cache=False,  # a fresh check every run; restarts reuse self._era
            )
        except ConnectError as err:
            if err.stage == "start":
                self.rec.add(bucket, "server-start", str(err))
                return False
            detail = self._stderr(self.transport) if self.transport is not None else []
            if isinstance(err.cause, RequestTimeout):
                self.rec.add(bucket, "initialize-timeout", str(err), detail=detail)
            else:
                self.rec.add(bucket, "initialize-error", str(err), detail=detail)
            return False
        self.transport = t
        if first:
            self.result.startup_ms = _ms(start)
            self._era = hs.era
            self.result.era = hs.label()
            self.result.supported_versions = list(hs.supported_versions)
            self.result.era_note = hs.fallback
            if hs.late_discover:
                self.result.era_note = ("server/discover was answered only after the timeout (a slow start); "
                                        "started the server again in the modern era")
            if hs.era == "legacy":
                self._check_initialize(hs.initialize)
                if hs.discover_crashed or isinstance(hs.discover_error, RequestTimeout):
                    what = "exited" if hs.discover_crashed else "did not answer"
                    self.rec.add(bucket, "discover-unanswered",
                                 f"the server {what} on server/discover; dual-era clients only fall back to "
                                 "initialize after a timeout or a restart (answer unknown methods with -32601)")
            else:
                self._check_discover(hs)
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

    def _check_discover(self, hs: Handshake) -> None:
        """A modern server: check its DiscoverResult, and how it treats legacy clients and bad requests."""
        bucket = self.result.findings
        self._caps = dict(hs.capabilities)
        self.result.protocol_version = hs.protocol_version
        self.result.server_info = dict(hs.server_info)
        if hs.discover_missing:
            self.rec.add(bucket, "discover-missing",
                         f"server/discover failed ({hs.discover_error}), but modern requests work; "
                         "servers MUST implement server/discover")
        else:
            res = hs.discover
            if not isinstance(res, dict):
                self.rec.add(bucket, "discover-invalid",
                             f"server/discover result is {type(res).__name__}, not an object")
            else:
                versions = res.get("supportedVersions")
                if not isinstance(versions, list) or not versions or not all(isinstance(v, str) for v in versions):
                    self.rec.add(bucket, "discover-invalid",
                                 "server/discover result needs a non-empty `supportedVersions` list of strings")
                if not isinstance(res.get("capabilities"), dict):
                    self.rec.add(bucket, "discover-invalid", "server/discover result has no `capabilities` object")
                self._check_modern_result("server/discover", res, bucket, cacheable=True)
                if not hs.server_info:
                    self.rec.add(bucket, "server-info-missing",
                                 f"server/discover result has no _meta[\"{META_SERVER_INFO}\"] (name, version)")
                if hs.supported_versions and not any(v in MODERN_VERSIONS for v in hs.supported_versions):
                    self.rec.add(bucket, "protocol-version",
                                 f"server supports {', '.join(hs.supported_versions)}; this probe speaks "
                                 + ", ".join(MODERN_VERSIONS + LEGACY_VERSIONS))
        if self.opt.era_checks:
            if self.opt.era == "auto":
                self._check_legacy_client()
            self._check_error_codes()

    def _check_modern_result(self, what: str, result: Any, bucket: List[Finding], cacheable: bool = False,
                             tool: Optional[str] = None) -> None:
        """resultType on every modern result; ttlMs and cacheScope on cacheable ones (lists, discover)."""
        if not isinstance(result, dict):
            return
        kind = result.get("resultType")
        if kind is None:
            self.rec.add(bucket, "result-type-missing", f"{what} result has no `resultType`", tool)
        elif kind not in RESULT_TYPES:
            self.rec.add(bucket, "result-type-missing",
                         f"{what} resultType {kind!r} is not \"complete\" or \"input_required\"", tool)
        if not cacheable:
            return
        missing = []
        ttl = result.get("ttlMs")
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl < 0:
            missing.append("no ttlMs" if ttl is None else f"ttlMs {ttl!r} is not a number >= 0")
        scope = result.get("cacheScope")
        if scope not in CACHE_SCOPES:
            missing.append("no cacheScope" if scope is None else f"cacheScope {scope!r} is not \"public\" or \"private\"")
        if missing:
            self.rec.add(bucket, "cache-fields-missing", f"{what} result: " + "; ".join(missing), tool)

    def _check_legacy_client(self) -> None:
        """Open a second connection the old way (initialize only): dual-era servers answer it."""
        bucket = self.result.findings
        t = self.factory()
        t.client_info = {"name": "amagi-mcp-probe", "version": __version__}
        params = {"protocolVersion": self.opt.protocol_version, "capabilities": {},
                  "clientInfo": dict(t.client_info)}
        try:
            t.start()
            # a fresh start: give it the same time as the first one
            init = t.request("initialize", params, timeout=self.opt.startup_timeout, modern=False)
        except RpcError as exc:
            self.result.era = "modern"
            named = supported_from(exc.data) or [v for v in MODERN_VERSIONS if v in str(exc.message)]
            if exc.code != UNSUPPORTED_VERSION or not named:
                self.rec.add(bucket, "legacy-rejected-unhelpful",
                             f"initialize (a legacy client) got {exc}; a modern-only server SHOULD answer "
                             "-32022 naming its supported versions, the only hint a legacy client can show")
            return
        except TransportError as exc:
            self.result.era = "modern"
            self.rec.add(bucket, "legacy-rejected-unhelpful",
                         f"initialize (a legacy client) got no JSON-RPC answer ({exc}); a modern-only server "
                         "SHOULD answer -32022 naming its supported versions")
            return
        finally:
            try:
                t.close()
            except Exception:
                pass
        if isinstance(init, dict) and isinstance(init.get("protocolVersion"), str):
            self.result.era = "dual"
            if init["protocolVersion"] not in self.result.supported_versions:
                self.result.supported_versions.append(init["protocolVersion"])
        else:
            self.result.era = "modern"

    def _check_error_codes(self) -> None:
        """-32022 (HTTP 400) for an unsupported version; -32601 (HTTP 404) for an unknown method."""
        t = self.transport
        assert t is not None
        bucket = self.result.findings
        http = self.result.transport == "http"
        wait = min(5.0, self.opt.timeout)
        cases = [
            ("server/discover", BOGUS_VERSION, UNSUPPORTED_VERSION, 400,
             f"server/discover declaring version {BOGUS_VERSION}"),
            (BOGUS_METHOD, None, -32601, 404, f"unknown method {BOGUS_METHOD}"),
        ]
        for method, version, code, status, label in cases:
            try:
                got = t.request(method, {}, timeout=wait, version=version)
            except RpcError as exc:
                problems = []
                if exc.code != code:
                    problems.append(f"error code {exc.code}, expected {code}")
                elif code == UNSUPPORTED_VERSION:
                    if not supported_from(exc.data):
                        problems.append("error data has no `supported` version list")
                    requested = exc.data.get("requested") if isinstance(exc.data, dict) else None
                    if requested not in (None, version):
                        problems.append(f"data.requested is {requested!r}, not {version!r}")
                if http and exc.http_status != status:
                    problems.append(f"HTTP {exc.http_status}, expected {status}")
                if problems:
                    self.rec.add(bucket, "error-code", f"{label}: " + "; ".join(problems))
                continue
            except HttpStatusError as exc:
                self.rec.add(bucket, "error-code", f"{label}: HTTP {exc.status} with no JSON-RPC error body; "
                                                   f"expected HTTP {status} and error {code}")
                continue
            except ServerExited as exc:
                self._harvest()
                self.rec.add(bucket, "error-code", f"{label}: {exc}; expected error {code}", detail=self._stderr(t))
                if not self._restart(label):
                    return
                t = self.transport
                assert t is not None
                continue
            except RequestTimeout:
                self.rec.add(bucket, "error-code", f"{label}: no answer within {wait:g}s; expected error {code}")
                continue
            except TransportError as exc:
                self.rec.add(bucket, "error-code", f"{label}: {exc}")
                continue
            self.rec.add(bucket, "error-code",
                         f"{label}: the server answered with a result; expected error {code}")

    def _stderr(self, t: Transport) -> List[str]:
        """The last few stderr lines: for a Python traceback, that is the exception itself."""
        return shorten_paths([line for line in t.stderr_tail(40) if line.strip()][-8:], _bases(t))

    def _harvest(self) -> None:
        """Keep stdout noise and stderr from a session before it is replaced."""
        t = self.transport
        if t is None:
            return
        for line in t.noise:
            if line not in self._noise_seen:
                self._noise_seen.append(line)
        self._stderr_seen = shorten_paths(t.stderr_tail(30), _bases(t)) or self._stderr_seen

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
        self._era: Optional[str] = None
        try:
            if not self._connect(first=True):
                return self.result
            tools = self._list_tools()
            if tools is None:
                return self.result
            if tools and "tools" not in self._caps:
                where = "server/discover" if self._era == "modern" else "initialize"
                self.rec.add(self.result.findings, "no-tools-capability",
                             f"tools/list returned tools, but {where} did not declare the `tools` capability")
            self._check_definitions(tools)
            self._call_all(tools)
        finally:
            self._close()
            loose = [line for line in self._noise_seen if line not in self._noise_blamed]
            if loose:
                self.rec.add(self.result.findings, "stdout-noise",
                             f"{len(loose)} {self._noise_where()} were not JSON-RPC", detail=loose[:5])
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
            if self._era == "modern":
                self._check_modern_result("tools/list", page, bucket, cacheable=True)
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
            reason = self._skip_reason(tool, tr)
            if reason:
                tr.status = "skip"
                tr.skip_reason = reason
                continue
            if stopped:
                tr.status = "not-run"
                tr.skip_reason = "server could not be restarted"
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
                    hint = " (args match inputSchema: schema and handler disagree)"
                self.rec.add(tr.findings, "call-rpc-error", f"{exc}{hint}", tr.name)
                continue
            except TransportError as exc:
                total += _ms(start)
                self.rec.add(tr.findings, "call-rpc-error", str(exc), tr.name)
                continue
            total += _ms(start)
            if self._era == "modern":
                self._check_modern_result("tools/call", result, tr.findings, tool=tr.name)
                if isinstance(result, dict) and result.get("resultType") == "input_required":
                    self.rec.add(tr.findings, "input-required",
                                 "the tool asked the client for more input (resultType input_required); "
                                 "the probe does not answer input requests", tr.name)
                    self._blame_noise(tr, noise_before)
                    continue
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
                         f"{len(new)} {self._noise_where()} were not JSON-RPC while this tool ran",
                         tr.name, detail=new[:3])

    def _noise_where(self) -> str:
        if self.result.transport == "http":
            return "event-stream message(s)"
        return "line(s) on stdout (log to stderr instead)"

    def _recover_after_timeout(self, name: str) -> bool:
        t = self.transport
        assert t is not None
        rid = getattr(t, "last_request_id", None)
        try:
            if rid is not None:
                t.notify("notifications/cancelled", {"requestId": rid, "reason": "amagi-mcp-probe timeout"})
            # modern servers have no ping; server/discover is the cheap request they all answer
            t.request("server/discover" if t.era == "modern" else "ping", {}, timeout=min(5.0, self.opt.timeout))
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

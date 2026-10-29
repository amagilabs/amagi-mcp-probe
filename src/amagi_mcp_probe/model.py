"""Result types and the catalogue of checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

ERROR = "error"
WARNING = "warning"

# check id -> (default severity, one-line description). Order is the order of --list-checks.
CHECKS: Dict[str, tuple] = {
    "server-start": (ERROR, "the server command could not be started, or the URL could not be reached"),
    "initialize-timeout": (ERROR, "no answer to `server/discover` or `initialize` within --startup-timeout"),
    "initialize-error": (ERROR, "the handshake failed: JSON-RPC error, or the server exited during it"),
    "initialize-invalid": (ERROR, "the `initialize` result is missing protocolVersion, capabilities or serverInfo"),
    "protocol-version": (WARNING, "the server answered with a protocol version this probe does not know"),
    "discover-unanswered": (WARNING, "legacy server crashed on, or never answered, `server/discover` (should be -32601)"),
    "discover-missing": (ERROR, "modern server does not implement `server/discover`"),
    "discover-invalid": (ERROR, "the `server/discover` result has no supportedVersions list or capabilities object"),
    "server-info-missing": (WARNING, "modern server sends no `_meta` serverInfo in its server/discover result"),
    "result-type-missing": (ERROR, "modern result has no `resultType` (or not complete / input_required)"),
    "cache-fields-missing": (ERROR, "modern tools/list or server/discover result lacks a valid ttlMs or cacheScope"),
    "error-code": (ERROR, "modern server: wrong error for a bad version (-32022, HTTP 400) or unknown method (-32601, HTTP 404)"),
    "legacy-rejected-unhelpful": (WARNING, "modern-only server rejects `initialize` without naming its versions (-32022)"),
    "input-required": (WARNING, "tool returned resultType input_required; the probe does not answer input requests"),
    "no-tools-capability": (WARNING, "the server lists tools but did not declare the `tools` capability"),
    "tools-list-error": (ERROR, "`tools/list` failed, timed out, or returned something that is not a tool list"),
    "stdout-noise": (ERROR, "the server wrote non-JSON-RPC text to stdout (breaks real clients)"),
    "server-crash": (ERROR, "the server process exited during a tool call"),
    "too-many-restarts": (ERROR, "the server crashed or hung too often; remaining tools were not called"),
    "tool-name": (WARNING, "tool name is not 1-128 chars of A-Z a-z 0-9 _ - ."),
    "tool-name-duplicate": (ERROR, "two tools share one name"),
    "tool-description": (WARNING, "tool has no description"),
    "input-schema-missing": (ERROR, "tool has no inputSchema"),
    "input-schema-type": (ERROR, "inputSchema is not `type: object`"),
    "input-schema-invalid": (ERROR, "inputSchema is not a valid JSON Schema"),
    "required-not-defined": (WARNING, "a `required` property is not listed in `properties`"),
    "schema-note": (WARNING, "a schema uses an unknown $schema dialect or a regex Python cannot check"),
    "output-schema-invalid": (ERROR, "outputSchema is not a valid object JSON Schema"),
    "args-unsatisfiable": (WARNING, "no valid sample arguments could be generated, so the tool was not called"),
    "args-override-invalid": (WARNING, "arguments from --args do not match the tool's inputSchema"),
    "call-timeout": (ERROR, "`tools/call` got no answer within --timeout"),
    "call-rpc-error": (ERROR, "`tools/call` with schema-valid arguments returned a JSON-RPC error"),
    "call-result-invalid": (ERROR, "the tool result is not a valid CallToolResult"),
    "tool-error": (WARNING, "the tool returned isError: true for schema-valid arguments"),
    "structured-content-missing": (ERROR, "tool declares outputSchema but returned no structuredContent"),
    "structured-content-invalid": (ERROR, "structuredContent does not match the tool's outputSchema"),
    "content-type-unknown": (WARNING, "a content block has a type this probe does not know"),
}


@dataclass
class Finding:
    check: str
    severity: str
    message: str
    tool: Optional[str] = None
    detail: List[str] = field(default_factory=list)


@dataclass
class ToolResult:
    name: str
    status: str = "pass"  # pass | warn | fail | skip | not-run
    duration_ms: Optional[int] = None
    arguments: Any = None
    args_source: Optional[str] = None  # generated | --args
    skip_reason: Optional[str] = None
    findings: List[Finding] = field(default_factory=list)

    def finalize(self) -> None:
        if self.status in ("skip", "not-run") and not any(f.severity == ERROR for f in self.findings):
            return
        if any(f.severity == ERROR for f in self.findings):
            self.status = "fail"
        elif self.findings:
            self.status = "warn"
        else:
            self.status = "pass"


@dataclass
class ServerResult:
    name: str
    target: str
    transport: str
    protocol_version: Optional[str] = None
    era: Optional[str] = None  # modern | dual | legacy (MCP 2026-07-28 vs the initialize handshake)
    supported_versions: List[str] = field(default_factory=list)
    era_note: Optional[str] = None  # why the probe fell back to initialize
    server_info: Dict[str, Any] = field(default_factory=dict)
    startup_ms: Optional[int] = None
    findings: List[Finding] = field(default_factory=list)
    tools: List[ToolResult] = field(default_factory=list)
    restarts: int = 0
    stderr_tail: List[str] = field(default_factory=list)

    def all_findings(self) -> List[Finding]:
        out = list(self.findings)
        for t in self.tools:
            out.extend(t.findings)
        return out

    def counts(self) -> Dict[str, int]:
        c = {"pass": 0, "warn": 0, "fail": 0, "skip": 0, "not-run": 0}
        for t in self.tools:
            c[t.status] = c.get(t.status, 0) + 1
        return c

    def failed(self, strict: bool = False) -> bool:
        for f in self.all_findings():
            if f.severity == ERROR or (strict and f.severity == WARNING):
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

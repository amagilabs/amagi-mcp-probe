"""Render probe results as text, JSON, Markdown, GitHub annotations, or JUnit XML."""

from __future__ import annotations

import json
from typing import Any, Dict, List
from xml.sax.saxutils import escape, quoteattr

from . import __version__
from .model import ERROR, WARNING, Finding, ServerResult, ToolResult

LABEL = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP", "not-run": "----"}


def _short(value: Any, limit: int = 70) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _verdict(results: List[ServerResult], strict: bool) -> str:
    return "FAIL" if any(r.failed(strict) for r in results) else "PASS"


def _summary_line(r: ServerResult) -> str:
    c = r.counts()
    total = len(r.tools)
    parts = [f"{c['pass']} passed"]
    if c["warn"]:
        parts.append(f"{c['warn']} warned")
    if c["fail"]:
        parts.append(f"{c['fail']} failed")
    if c["skip"]:
        parts.append(f"{c['skip']} skipped")
    if c["not-run"]:
        parts.append(f"{c['not-run']} not run")
    return f"{total} tool{'s' if total != 1 else ''}: " + ", ".join(parts)


def _ms(ms: int) -> str:
    return "<1 ms" if ms < 1 else f"{ms} ms"


def _era(r: ServerResult) -> str:
    """'era dual (2026-07-28, 2025-11-25)': the MCP era and the versions the server listed."""
    if not r.era:
        return ""
    versions = f" ({', '.join(r.supported_versions)})" if r.supported_versions else ""
    return f"era {r.era}{versions}"


def _finding_line(f: Finding) -> str:
    sev = "error" if f.severity == ERROR else "warn"
    return f"{sev}  {f.check}: {f.message}"


# text ----------------------------------------------------------------------

def render_text(results: List[ServerResult], strict: bool = False, verbose: bool = False) -> str:
    out: List[str] = []
    for r in results:
        info = r.server_info
        who = " ".join(str(info[k]) for k in ("name", "version") if k in info) or "unknown server"
        out.append(f"{r.name}: {r.target}")
        if r.protocol_version or r.startup_ms is not None:
            era = f", {_era(r)}" if r.era else ""
            ms = f", ready in {r.startup_ms} ms" if r.startup_ms is not None else ""
            out.append(f"  {who}, protocol {r.protocol_version or '?'}{era}{ms}")
        for f in r.findings:
            out.append("  " + _finding_line(f))
            out.extend("      " + d for d in f.detail[:8])
        if r.tools:
            out.append("")
            width = min(max(len(t.name) for t in r.tools), 36)
            for t in r.tools:
                out.extend(_tool_lines(t, width, verbose))
        if r.restarts:
            out.append(f"  server restarted {r.restarts} time{'s' if r.restarts != 1 else ''} after a crash or hang")
        out.append("")
        verdict = "FAIL" if r.failed(strict) else "PASS"
        if r.tools or not r.findings:
            out.append(f"{_summary_line(r)}.  {verdict}")
        else:
            out.append(f"server check failed.  {verdict}")
        out.append("")
    if len(results) > 1:
        bad = sum(1 for r in results if r.failed(strict))
        out.append(f"{len(results)} servers, {bad} failed.  {_verdict(results, strict)}")
    return "\n".join(out).rstrip() + "\n"


def _tool_lines(t: ToolResult, width: int, verbose: bool) -> List[str]:
    label = LABEL.get(t.status, t.status.upper())
    note = ""
    if t.status in ("skip", "not-run"):
        note = t.skip_reason or ""
    elif t.duration_ms is not None:
        note = _ms(t.duration_ms)
    lines = [f"  {label}  {t.name:<{width}}  {note}".rstrip()]
    if verbose and t.arguments is not None:
        lines.append(f"        args ({t.args_source}): {_short(t.arguments, 90)}")
    for f in t.findings:
        lines.append("        " + _finding_line(f))
        lines.extend("          " + d for d in f.detail[:8])
    return lines


# json ----------------------------------------------------------------------

def render_json(results: List[ServerResult], strict: bool = False) -> str:
    doc = {
        "tool": "amagi-mcp-probe",
        "version": __version__,
        "verdict": _verdict(results, strict),
        "strict": strict,
        "servers": [_server_dict(r, strict) for r in results],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _server_dict(r: ServerResult, strict: bool) -> Dict[str, Any]:
    d = r.to_dict()
    d["verdict"] = "FAIL" if r.failed(strict) else "PASS"
    d["counts"] = r.counts()
    return d


# markdown --------------------------------------------------------------------

def render_markdown(results: List[ServerResult], strict: bool = False) -> str:
    out = [f"## amagi-mcp-probe: {_verdict(results, strict)}", ""]
    for r in results:
        verdict = "FAIL" if r.failed(strict) else "PASS"
        info = r.server_info
        who = " ".join(str(info[k]) for k in ("name", "version") if k in info)
        out.append(f"### {r.name}: {verdict}")
        out.append("")
        meta = [f"`{_md(r.target)}`"]
        if who:
            meta.append(who)
        if r.protocol_version:
            meta.append(f"protocol {r.protocol_version}")
        if r.era:
            meta.append(_era(r))
        out.append(" · ".join(meta))
        out.append("")
        if r.findings:
            for f in r.findings:
                out.append(f"- **{f.severity}** `{f.check}`: {_md(f.message)}")
            out.append("")
        if r.tools:
            out.append("| Tool | Result | Time | Findings |")
            out.append("|------|--------|------|----------|")
            for t in r.tools:
                time_ms = _ms(t.duration_ms) if t.duration_ms is not None else ""
                notes = "<br>".join(f"`{f.check}` {_md(f.message)}" for f in t.findings)
                if not notes and t.skip_reason:
                    notes = _md(t.skip_reason)
                out.append(f"| `{_md(t.name)}` | {LABEL.get(t.status, t.status)} | {time_ms} | {notes} |")
            out.append("")
            out.append(_summary_line(r) + ".")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


# github annotations ------------------------------------------------------------

def render_github(results: List[ServerResult], strict: bool = False) -> str:
    out = []
    for r in results:
        for f in r.all_findings():
            level = "error" if f.severity == ERROR or strict else "warning"
            where = f"{r.name} / {f.tool}" if f.tool else r.name
            msg = f"{where}: {f.check}: {f.message}"
            out.append(f"::{level} title=amagi-mcp-probe::{_gh(msg)}")
    out.append(render_text(results, strict).rstrip())
    return "\n".join(out) + "\n"


def _gh(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


# junit ------------------------------------------------------------------------

def render_junit(results: List[ServerResult], strict: bool = False) -> str:
    suites = []
    total_tests = total_fail = total_skip = 0
    for r in results:
        cases = []
        server_fail = [f for f in r.findings if f.severity == ERROR or (strict and f.severity == WARNING)]
        cases.append(_case(r.name, "server", r.startup_ms, server_fail, r.findings, None))
        for t in r.tools:
            failing = [f for f in t.findings if f.severity == ERROR or (strict and f.severity == WARNING)]
            skipped = t.skip_reason if t.status in ("skip", "not-run") and not failing else None
            cases.append(_case(r.name, f"tool {t.name}", t.duration_ms, failing, t.findings, skipped))
        fails = sum(1 for c in cases if c[1] == "fail")
        skips = sum(1 for c in cases if c[1] == "skip")
        total_tests += len(cases)
        total_fail += fails
        total_skip += skips
        body = "\n".join(c[0] for c in cases)
        props = ""
        if r.era:
            items = [("era", r.era), ("protocolVersion", r.protocol_version or ""),
                     ("supportedVersions", ",".join(r.supported_versions))]
            props = ("    <properties>" + "".join(f"<property name={quoteattr(k)} value={quoteattr(v)}/>"
                                                   for k, v in items) + "</properties>\n")
        suites.append(
            f'  <testsuite name={quoteattr("mcp-probe." + r.name)} tests="{len(cases)}" '
            f'failures="{fails}" errors="0" skipped="{skips}">\n{props}{body}\n  </testsuite>'
        )
    head = (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuites name="amagi-mcp-probe" '
            f'tests="{total_tests}" failures="{total_fail}" errors="0" skipped="{total_skip}">')
    return head + "\n" + "\n".join(suites) + "\n</testsuites>\n"


def _case(server: str, name: str, ms, failing: List[Finding], all_findings: List[Finding], skipped):
    secs = f"{(ms or 0) / 1000:.3f}"
    attrs = f'classname={quoteattr("mcp-probe." + server)} name={quoteattr(name)} time="{secs}"'
    inner = []
    state = "pass"
    if failing:
        state = "fail"
        msg = "; ".join(f"{f.check}: {f.message}" for f in failing)
        text = "\n".join(f"{f.check}: {f.message}" + "".join("\n  " + d for d in f.detail) for f in failing)
        inner.append(f"<failure message={quoteattr(msg[:500])}>{escape(text)}</failure>")
    elif skipped:
        state = "skip"
        inner.append(f"<skipped message={quoteattr(skipped)}/>")
    warns = [f for f in all_findings if f not in failing]
    if warns:
        inner.append("<system-out>" + escape("\n".join(_finding_line(f) for f in warns)) + "</system-out>")
    if inner:
        return (f"    <testcase {attrs}>" + "".join(inner) + "</testcase>", state)
    return (f"    <testcase {attrs}/>", state)


RENDERERS = {
    "text": render_text,
    "json": render_json,
    "markdown": render_markdown,
    "github": render_github,
}

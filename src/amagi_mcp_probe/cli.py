"""Command line entry point."""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from typing import List, Optional

from . import __version__
from .config import ConfigError, ServerSpec, load_args, load_servers
from .model import CHECKS, Finding, ServerResult
from .probe import LATEST_PROTOCOL, Options, Prober
from .report import RENDERERS, render_junit, render_markdown, render_text
from .transport import HttpTransport, StdioTransport

EPILOG = """examples:
  amagi-mcp-probe -- python my_server.py
  amagi-mcp-probe -- npx -y @modelcontextprotocol/server-everything
  amagi-mcp-probe --url http://localhost:3001/mcp
  amagi-mcp-probe --config .mcp.json --server notes
  amagi-mcp-probe --junit probe.xml --summary "$GITHUB_STEP_SUMMARY" -- uv run server.py

exit codes: 0 no errors, 1 errors found (or warnings with --strict), 2 usage or config error
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amagi-mcp-probe",
        description="Smoke-test an MCP server: start it, check every tool schema, call every tool "
                    "with generated arguments, report crashes, hangs and schema violations.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("command", nargs="*", help="server command to launch over stdio (put it after --)")
    src = p.add_argument_group("server")
    src.add_argument("--url", help="probe a Streamable HTTP server at this URL instead of launching one")
    src.add_argument("--header", action="append", default=[], metavar="'NAME: VALUE'",
                     help="extra HTTP header for --url (repeatable)")
    src.add_argument("--config", metavar="FILE",
                     help="read servers from an MCP client config (.mcp.json, claude_desktop_config.json, "
                          ".vscode/mcp.json)")
    src.add_argument("--server", action="append", default=[], metavar="NAME",
                     help="with --config: probe only this server (repeatable; default all)")
    src.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                     help="environment variable for the server process (repeatable)")
    src.add_argument("--cwd", metavar="DIR", help="working directory for the server process")
    run = p.add_argument_group("what to run")
    run.add_argument("--timeout", type=float, default=30.0, metavar="SEC",
                     help="seconds to wait for each tools/list and tools/call (default 30)")
    run.add_argument("--startup-timeout", type=float, default=30.0, metavar="SEC",
                     help="seconds to wait for the handshake: server/discover or initialize (default 30)")
    run.add_argument("--only", action="append", default=[], metavar="GLOB", help="call only matching tools")
    run.add_argument("--skip", action="append", default=[], metavar="GLOB", help="do not call matching tools")
    run.add_argument("--no-call", action="store_true", help="check schemas only, call nothing")
    run.add_argument("--allow-destructive", action="store_true",
                     help="also call tools annotated destructiveHint: true (skipped by default)")
    run.add_argument("--include-optional", action="store_true",
                     help="fill optional properties too, not just required ones")
    run.add_argument("--args", metavar="FILE",
                     help='JSON file of real arguments per tool: {"tool": {...}} or {"tool": [{...}, {...}]}')
    run.add_argument("--protocol-version", default=LATEST_PROTOCOL, metavar="VER",
                     help=f"protocol version to request in initialize (default {LATEST_PROTOCOL})")
    run.add_argument("--era", choices=["auto", "modern", "legacy"], default="auto",
                     help="auto: try MCP 2026-07-28 server/discover, fall back to initialize (default); "
                          "modern or legacy: skip the other")
    out = p.add_argument_group("output")
    out.add_argument("--format", choices=sorted(RENDERERS), default="text", help="stdout format (default text)")
    out.add_argument("--junit", metavar="FILE", help="also write a JUnit XML report")
    out.add_argument("--summary", metavar="FILE",
                     help="also append a Markdown report (pass $GITHUB_STEP_SUMMARY in GitHub Actions)")
    out.add_argument("-v", "--verbose", action="store_true", help="show the arguments sent to each tool")
    out.add_argument("--strict", action="store_true", help="warnings fail the run too")
    out.add_argument("--ignore", action="append", default=[], metavar="CHECK",
                     help="drop findings of this check id (repeatable); see --list-checks")
    out.add_argument("--list-checks", action="store_true", help="print every check id and exit")
    p.add_argument("--version", action="version", version=f"amagi-mcp-probe {__version__}")
    return p


def _split(argv: List[str]):
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


_LAUNCHERS = {"npx", "uvx", "uv", "node", "bun", "bunx", "deno", "pipx", "pnpm", "yarn", "docker", "py", "java"}
_SUBCOMMANDS = {"run", "x", "exec", "tool", "dlx"}
_GENERIC = {"index", "main", "__main__", "server", "cli", "app"}
_BUILD_DIRS = {"dist", "build", "src", "lib", "bin", "out"}


def server_label(command: List[str]) -> str:
    """A short name for a server command: the script or package it runs."""
    exe = os.path.splitext(re.split(r"[\\/]", command[0])[-1])[0].lower()
    target = command[0]
    if exe in _LAUNCHERS or exe.startswith("python"):
        target = next((a for a in command[1:] if not a.startswith("-") and a not in _SUBCOMMANDS), command[0])
    parts = [p for p in re.split(r"[\\/]", target) if p]
    if not parts:
        return "server"
    name = parts[-1]
    if os.path.splitext(name)[0] in _GENERIC:
        for parent in reversed(parts[:-1]):
            if parent not in _BUILD_DIRS:
                return parent
    return name


def _usage(parser: argparse.ArgumentParser, msg: str) -> int:
    parser.print_usage(sys.stderr)
    print(f"amagi-mcp-probe: error: {msg}", file=sys.stderr)
    return 2


def _specs(args, command: List[str], parser) -> List[ServerSpec]:
    sources = sum(bool(x) for x in (command, args.url, args.config))
    if sources == 0:
        raise ConfigError("give a server command after --, or --url, or --config")
    if sources > 1:
        raise ConfigError("use only one of: a command after --, --url, --config")
    env = {}
    for item in args.env:
        if "=" not in item:
            raise ConfigError(f"--env needs KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        env[k] = v
    headers = {}
    for item in args.header:
        if ":" not in item:
            raise ConfigError(f"--header needs 'Name: value', got {item!r}")
        k, v = item.split(":", 1)
        headers[k.strip()] = v.strip()
    if command:
        return [ServerSpec(name=server_label(command), kind="stdio", command=command, env=env, cwd=args.cwd)]
    if args.url:
        return [ServerSpec(name="server", kind="http", url=args.url, headers=headers)]
    specs = load_servers(args.config)
    if args.server:
        known = {s.name for s in specs}
        missing = [n for n in args.server if n not in known]
        if missing:
            raise ConfigError(f"not in {args.config}: {', '.join(missing)} (found: {', '.join(sorted(known))})")
        specs = [s for s in specs if s.name in args.server]
    for s in specs:
        s.env.update(env)
        if args.cwd:
            s.cwd = args.cwd
        s.headers.update(headers)
    return specs


def _run_spec(spec: ServerSpec, options: Options) -> ServerResult:
    if spec.unsupported:
        r = ServerResult(name=spec.name, target=spec.url or "", transport=spec.kind)
        r.findings.append(Finding(check="server-start", severity="error", message=spec.unsupported))
        return r
    if spec.kind == "http":
        def factory():
            return HttpTransport(spec.url or "", spec.headers)
    else:
        def factory():
            return StdioTransport(spec.command, env=spec.env, cwd=spec.cwd)
    return Prober(spec.name, factory, options, spec.kind).run()


def _write(path: str, text: str, append: bool = False) -> None:
    with open(path, "a" if append else "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main(argv: Optional[List[str]] = None) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    opts_argv, command = _split(argv)
    parser = build_parser()
    args = parser.parse_args(opts_argv)
    if args.list_checks:
        width = max(len(k) for k in CHECKS)
        for check, (sev, desc) in CHECKS.items():
            print(f"{check:<{width}}  {sev:<7}  {desc}")
        return 0
    command = list(args.command) + command
    unknown = [c for c in args.ignore if c not in CHECKS]
    if unknown:
        return _usage(parser, f"unknown check id for --ignore: {', '.join(unknown)} (see --list-checks)")
    if args.timeout <= 0 or args.startup_timeout <= 0:
        return _usage(parser, "timeouts must be positive")
    try:
        specs = _specs(args, command, parser)
        tool_args = load_args(args.args) if args.args else {}
    except ConfigError as exc:
        return _usage(parser, str(exc))

    options = Options(
        timeout=args.timeout,
        startup_timeout=args.startup_timeout,
        only=args.only,
        skip=args.skip,
        call=not args.no_call,
        allow_destructive=args.allow_destructive,
        include_optional=args.include_optional,
        args=tool_args,
        ignore=set(args.ignore),
        protocol_version=args.protocol_version,
        era=args.era,
    )
    results = [_run_spec(s, options) for s in specs]

    if args.format == "text":
        sys.stdout.write(render_text(results, args.strict, args.verbose))
    else:
        sys.stdout.write(RENDERERS[args.format](results, args.strict))
    sys.stdout.flush()
    try:
        if args.junit:
            _write(args.junit, render_junit(results, args.strict))
        if args.summary:
            _write(args.summary, render_markdown(results, args.strict), append=True)
    except OSError as exc:
        print(f"amagi-mcp-probe: could not write report: {exc}", file=sys.stderr)
        return 2
    return 1 if any(r.failed(args.strict) for r in results) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

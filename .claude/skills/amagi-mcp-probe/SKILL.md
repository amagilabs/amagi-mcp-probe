---
name: amagi-mcp-probe
description: Smoke-test an MCP server. Starts it over stdio (or connects over Streamable HTTP), validates every tool's inputSchema and outputSchema, calls every tool with schema-valid generated arguments, and reports crashes, timeouts, stdout pollution, JSON-RPC errors, malformed results, and structuredContent that does not match outputSchema. Speaks MCP 2026-07-28 (server/discover) and the older initialize handshake, reports which era each server uses, and checks modern servers for discover, resultType, ttlMs/cacheScope and the right error codes. Use when building or changing an MCP server, before publishing one, when a client says a server "won't connect" or a tool "does nothing", or when adding an MCP check to CI.
---

# amagi-mcp-probe

A CLI that launches an MCP server, lists its tools, checks their schemas, calls each tool once, and
exits 1 if anything is broken.

## When to use this skill

- You just added or changed a tool in an MCP server and want to know it still starts and answers.
- A client (Claude Code, Claude Desktop, an IDE) fails to connect, or shows the server with no tools.
  Stray `print` output on stdout is the most common cause, and the probe names the tool that wrote it.
- A tool works by hand but a model's calls fail: the published schema and the handler often disagree.
- The user wants a CI step that fails a pull request when the server breaks.
- The user is moving a server to MCP 2026-07-28, or wants to know which protocol era it speaks.

## How to use it

Install once: `pipx install amagi-mcp-probe` (or `pip install -e .` from this repo in development).

```bash
amagi-mcp-probe -- python server.py                 # stdio server: the launch command goes after --
amagi-mcp-probe -- npx -y some-mcp-server
amagi-mcp-probe --url http://localhost:3001/mcp     # Streamable HTTP server
amagi-mcp-probe --config .mcp.json --server NAME    # a server from a client config
amagi-mcp-probe --format json -- python server.py   # machine-readable report
amagi-mcp-probe --no-call -- python server.py       # schemas only, call nothing
amagi-mcp-probe -v -- python server.py              # show the arguments sent to each tool
amagi-mcp-probe --era legacy -- python server.py    # skip era detection: modern or legacy
```

Exit code 0 means no errors, 1 means errors were found (or warnings, with `--strict`), 2 means a usage
or config error.

Tools really run. Point a server that touches files, email, payments, or databases at a sandbox
(`--cwd`, `--env`) before probing it. Tools annotated `destructiveHint: true` are skipped unless you
pass `--allow-destructive`.

## Working with the results

1. Run with `--format json`. Each server in `servers` has `era` (`modern`, `dual` or `legacy`),
   `protocol_version`, `supported_versions`, `findings` (server-level), `tools` (each with
   `name`, `status`, `arguments`, `args_source`, `findings`), `restarts`, and `stderr_tail`. Each
   finding has `check`, `severity`, `message`, `tool`, and `detail`.
2. Fix errors first. What each check usually means:
   - `stdout-noise`: something printed to stdout. Send logs to stderr.
   - `server-crash`: `detail` holds the tail of the server's stderr, usually a traceback.
   - `call-timeout`: the handler blocked or never replied. Check for a missing `await` or a deadlock.
   - `call-rpc-error` on schema-valid arguments: the handler rejects input its own `inputSchema`
     allows. Tighten the schema or relax the handler, so the two agree.
   - `structured-content-invalid` / `structured-content-missing`: the result doesn't match the tool's
     `outputSchema`.
   - `input-schema-invalid`, `input-schema-type`, `required-not-defined`: fix the schema itself.
   - Modern (2026-07-28) servers: `discover-missing` (implement `server/discover`),
     `result-type-missing` (add `resultType: "complete"`), `cache-fields-missing` (add `ttlMs` and
     `cacheScope` to the discover and `tools/list` results), `error-code` (an unsupported version
     must get -32022, an unknown method -32601; over HTTP, status 400 and 404).
   - `discover-unanswered`: a legacy server ignored or crashed on `server/discover`. It should answer
     -32601, so modern clients fall back without waiting.
3. `tool-error` warnings mean the tool returned `isError: true` for the generated arguments. That is
   often correct (an id that doesn't exist). Re-run with real values in an `--args` file,
   `{"tool_name": {...}}`, to confirm the tool works.
4. Re-run until it exits 0. Run `amagi-mcp-probe --list-checks` for every check id;
   `--ignore CHECK` drops one.

## Limits

- Transports: stdio and Streamable HTTP. The legacy HTTP+SSE transport is not supported.
- A tool that answers `resultType: "input_required"` is reported as a warning and not completed.
- The probe declares no client capabilities, so tools that need sampling, elicitation, or roots are
  not fully exercised. Tools that require task-based execution are skipped.
- One call per tool with minimal generated arguments (more with `--args`). It is a smoke test.

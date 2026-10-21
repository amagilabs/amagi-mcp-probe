# amagi-mcp-probe

Smoke-test any MCP server in one command: it starts the server, checks every tool schema, calls every tool with valid generated arguments, and fails your CI build on crashes, hangs, and schema violations.

![demo](assets/demo.gif)

## Install

```bash
pipx install amagi-mcp-probe
```

Python 3.9 or newer. The only dependency is jsonschema.

## Usage

Put the command that starts your server after `--`:

```bash
amagi-mcp-probe -- python my_server.py
amagi-mcp-probe -- npx -y @modelcontextprotocol/server-everything
amagi-mcp-probe --url http://localhost:3001/mcp
```

The probe runs the `initialize` handshake, pages through `tools/list`, validates each `inputSchema` and `outputSchema`, then calls each tool once with arguments built from its schema. Every result is checked as a `CallToolResult`, and `structuredContent` is checked against the tool's `outputSchema`.

Real output from `examples/servers/buggy_server.py`, a small server in this repo with one bug per tool:

```
$ amagi-mcp-probe --timeout 3 -- python examples/servers/buggy_server.py
buggy_server.py: python examples/servers/buggy_server.py
  buggy 0.3.0, protocol 2025-11-25, ready in 1312 ms

  PASS  echo          <1 ms
  FAIL  resize_image  156 ms
        error  server-crash: server process exited (code 1) while running this tool
              serve("buggy", "0.3.0", TOOLS)
              ~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^
            File "examples\servers\minimcp.py", line 65, in serve
              result = tool["handler"](params.get("arguments") or {})
            File "examples\servers\buggy_server.py", line 22, in resize_image
              scale = 100 / (args["width"] - 1)
                      ~~~~^~~~~~~~~~~~~~~~~~~~~
          ZeroDivisionError: division by zero
  FAIL  fetch_page    <1 ms
        error  stdout-noise: 1 line(s) on stdout (log to stderr instead) were not JSON-RPC while this tool ran
          fetching https://example.com/
  FAIL  weather       <1 ms
        error  structured-content-invalid: structuredContent (root): 'temp_c' is a required property
  FAIL  lookup_user   <1 ms
        error  call-rpc-error: JSON-RPC error -32602: user_id must be a string (args match inputSchema: schema and handler disagree)
  FAIL  slow_report   3014 ms
        warn  tool-description: no description; models pick tools by their description
        error  call-timeout: no answer within 3s
  FAIL  count_rows
        error  input-schema-invalid: properties/limit/type: 'int' is not valid under any of the given schemas
  SKIP  drop_table    destructiveHint: true (use --allow-destructive to call it)
  server restarted 2 times after a crash or hang

8 tools: 1 passed, 6 failed, 1 skipped.  FAIL
```

The exit code is 1. After the crash and the hang, the probe restarted the server and kept going, so one broken tool doesn't hide the rest. The crash detail is the tail of the server's stderr.

A well-behaved server looks like this:

```
$ amagi-mcp-probe -- python examples/servers/notes_server.py
notes_server.py: python examples/servers/notes_server.py
  notes 1.0.0, protocol 2025-11-25, ready in 2235 ms

  PASS  add_note      <1 ms
  WARN  get_note      <1 ms
        warn  tool-error: isError: true: no note with id note-000
  PASS  search_notes  <1 ms

3 tools: 2 passed, 1 warned.  PASS
```

`get_note` got a schema-valid id that doesn't exist, so the tool answered with `isError: true`. The probe counts that as a warning and the run still passes, because the server handled bad input the way MCP intends. Give it a real id with `--args` (below) and all three pass.

On the official reference servers, run on Windows with Node 24:

- `@modelcontextprotocol/server-everything` 2.0.0 over stdio: 16 tools, 16 passed.
- The same server over Streamable HTTP (`--url`): 16 tools, 16 passed.
- `@modelcontextprotocol/server-filesystem` 2.0.0 on a scratch folder: 14 tools, 8 passed, 3 warned (`read_file` on a path that doesn't exist), 3 skipped (`write_file`, `edit_file` and `move_file` are marked destructive).

### How arguments are generated

For each tool, the probe builds the dull, valid input a careful user would type: required properties only, the lowest allowed number (1 when the schema allows it), the first `enum` value, and a short string that matches `pattern`, `format`, and the length limits. It validates every generated value against the schema before sending it. If no valid value exists, the tool is reported as `args-unsatisfiable` and not called.

Pass `--include-optional` to fill optional properties too, and `-v` to print the arguments sent to each tool.

### Real arguments with `--args`

Generated arguments can't guess the id of a note that exists. Give the probe real values in a JSON file, one object per tool (or a list of objects to call a tool more than once):

```json
{"get_note": {"id": "note-001"}}
```

```
$ amagi-mcp-probe --strict --args args.json -- python examples/servers/notes_server.py
...
3 tools: 3 passed.  PASS
```

Arguments from the file are checked against the tool's `inputSchema` too. A mismatch is reported as `args-override-invalid`.

### Probe the servers in your client config

```bash
amagi-mcp-probe --config .mcp.json
amagi-mcp-probe --config claude_desktop_config.json --server notes
```

It reads the `mcpServers` format used by Claude Code and Claude Desktop, and the `servers` format in `.vscode/mcp.json`. `${VAR}` and `${VAR:-default}` in a config are expanded from your environment. Legacy SSE servers in a config are reported as unsupported.

### What it checks

`amagi-mcp-probe --list-checks` prints all 28 check ids. The main ones:

- Startup: the server starts, answers `initialize` within `--startup-timeout`, and returns `protocolVersion`, `capabilities`, and `serverInfo`.
- stdout: anything on stdout that isn't JSON-RPC (a stray `print`, a startup banner) is an error, because it breaks real clients. The probe tells you which tool was running when it appeared.
- Tool definitions: names, duplicates, missing descriptions, `inputSchema` that isn't a valid object schema, `required` properties that aren't defined, and invalid `outputSchema`.
- Calls: crashes, timeouts, JSON-RPC errors on schema-valid arguments, malformed results, `isError: true`, and `structuredContent` that is missing or doesn't match `outputSchema`.

Use `--ignore CHECK` to drop a check you don't care about.

### Options

```bash
amagi-mcp-probe --timeout 10 -- ...            # seconds per tools/list and tools/call (default 30)
amagi-mcp-probe --startup-timeout 60 -- ...    # seconds for the initialize handshake (default 30)
amagi-mcp-probe --only 'get_*' -- ...          # call only matching tools (repeatable)
amagi-mcp-probe --skip send_email -- ...       # never call these tools (repeatable)
amagi-mcp-probe --no-call -- ...               # check schemas only, call nothing
amagi-mcp-probe --allow-destructive -- ...     # also call tools marked destructiveHint: true
amagi-mcp-probe --env API_KEY=test -- ...      # environment for the server process (repeatable)
amagi-mcp-probe --cwd sandbox -- ...           # working directory for the server process
amagi-mcp-probe --url URL --header 'Authorization: Bearer x'   # Streamable HTTP server
amagi-mcp-probe --format json -- ...           # also: text (default), markdown, github
amagi-mcp-probe --junit probe.xml -- ...       # also write a JUnit XML report
amagi-mcp-probe --strict -- ...                # warnings fail the run too
```

Exit codes: 0 means no errors, 1 means errors were found (or warnings, with `--strict`), 2 means a usage or config error.

## Run it in CI

```yaml
# .github/workflows/mcp-probe.yml
name: mcp-probe
on: [push, pull_request]
jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install amagi-mcp-probe
      - run: pip install -e .   # your server
      - run: >
          amagi-mcp-probe --format github --junit probe.xml
          --summary "$GITHUB_STEP_SUMMARY"
          -- python -m my_server
```

`--format github` turns each finding into an error or warning annotation, `--summary` appends a Markdown table of every tool to the job summary page, and `--junit` writes a report that test-report actions can read. A Node server works the same way: add `actions/setup-node`, then put `node dist/index.js` after `--`.

The flags in this workflow are covered by the test suite. The workflow file itself hasn't been run on GitHub yet.

## Use it from Claude Code

This repo ships a skill at `.claude/skills/amagi-mcp-probe/SKILL.md`. Copy that folder into your own `.claude/skills/` and Claude Code can probe the server you're building, read the JSON report, and fix what failed.

## Limits

- It calls your tools for real. A tool that sends email, charges a card, or deletes files will do that, so point the server at a sandbox. Tools marked `destructiveHint: true` are skipped by default, but only if the server marks them.
- Transports: stdio and Streamable HTTP. The legacy HTTP+SSE transport is not supported.
- Generated arguments are minimal and know nothing about your data, so expect some `tool-error` warnings for lookups like `get_note`. Use `--args` for those.
- The probe declares no client capabilities, so tools that need sampling, elicitation, or roots from the client aren't fully exercised. Tools that require task-based execution are skipped.
- `pattern` regexes are checked with Python's `re`. A pattern that uses ECMA-262 syntax Python lacks (such as `\p{L}`) gets a `schema-note` warning instead of a false error.
- It makes one call per tool (more with `--args`). That catches crashes and schema mismatches, but edge cases deep inside a handler need your own tests or a fuzzer.

## If this saved you time

A star helps other people find it.

## More from AmagiLabs

- [amagilabs](https://github.com/amagilabs): other small open source dev tools from the same lab.
- [amagi-watch](https://github.com/amagilabs/amagi-watch): turn a video URL into a Claude-readable transcript and frame index.
- [legal-review-skills](https://github.com/amagilabs/legal-review-skills): Claude Code skills for contract and lease review.

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Amagisolutionsgroup, Inc. (AmagiLabs)

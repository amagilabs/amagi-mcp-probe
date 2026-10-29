# Changelog

## 0.2.0 (2026-10-29)

MCP 2026-07-28 support. The probe now speaks both protocol eras and tells you which one each server uses.

- The probe sends `server/discover` first. A DiscoverResult, or error -32022, means the server is modern. Any other error, a timeout, or an HTTP 4xx without a JSON-RPC error body means it is older, and the probe sends `initialize` instead.
- Modern requests carry `_meta` (protocol version, client info, client capabilities). Over HTTP they also carry the `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` headers, and no session id. There is no `notifications/initialized`.
- Reports show each server's era: `modern`, `dual` or `legacy`, with its supported versions. Text, Markdown, JSON (`era`, `supported_versions`, `era_note`) and JUnit (testsuite properties) all include it. The probe finds `dual` servers by opening a second, initialize-only connection.
- New checks for modern servers:
  - `discover-missing` and `discover-invalid`: `server/discover` is missing, or its result is malformed.
  - `server-info-missing`: no `serverInfo` in the discover result's `_meta`.
  - `result-type-missing`: a result has no `resultType`.
  - `cache-fields-missing`: the discover or `tools/list` result has no `ttlMs`/`cacheScope`.
  - `error-code`: an unsupported version doesn't get -32022 with HTTP 400, or an unknown method doesn't get -32601 with HTTP 404.
  - `legacy-rejected-unhelpful`: a modern-only server refuses `initialize` without naming its versions.
  - `input-required`: a tool answered `resultType: "input_required"`.
- New check for legacy servers: `discover-unanswered`, for a server that ignores or crashes on `server/discover` instead of answering -32601.
- `--era auto|modern|legacy` forces an era. The default is `auto`.
- A server that is slow to start (npx downloading it, say) can miss the `server/discover` timeout and get `initialize`. If its late discover answer names 2026-07-28, the probe starts it again in the modern era and says so in the report.
- When the server is modern, timeout recovery uses `server/discover` instead of `ping`.
- A JSON-RPC error inside an HTTP 200 reply now records that status code.
- Tests run against servers built on the official Python SDK: mcp 1.30 FastMCP (legacy) and mcp 2.3 MCPServer (dual and modern-only), over stdio and Streamable HTTP. Run `python tests/sdk_envs.py` to create the scratch venvs. Without them, those tests are skipped.

## 0.1.0 (2026-10-22)

First release: stdio and Streamable HTTP, schema checks, one call per tool with generated arguments, `--args`, `--config`, and text, JSON, Markdown, GitHub and JUnit output.

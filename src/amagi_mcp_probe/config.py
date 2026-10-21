"""Read MCP client config files (.mcp.json, claude_desktop_config.json, VS Code mcp.json)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class ConfigError(Exception):
    """A config or --args file is missing or malformed."""


@dataclass
class ServerSpec:
    name: str
    kind: str  # "stdio" | "http"
    command: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    unsupported: Optional[str] = None


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value: str) -> str:
    """Expand ${VAR} and ${VAR:-default} from the environment (Claude Code .mcp.json style)."""

    def sub(m: "re.Match[str]") -> str:
        name, default = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        return default if default is not None else m.group(0)

    return _VAR.sub(sub, value)


def _load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"file not found: {path}")
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}")


def load_servers(path: str) -> List[ServerSpec]:
    data = _load_json(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    servers = data.get("mcpServers")
    if servers is None:
        servers = data.get("servers")  # VS Code .vscode/mcp.json
    if servers is None and isinstance(data.get("mcp"), dict):
        servers = data["mcp"].get("servers")
    if not isinstance(servers, dict) or not servers:
        raise ConfigError(f"{path}: no \"mcpServers\" (or \"servers\") object found")
    base = os.path.dirname(os.path.abspath(path))
    out = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: server {name!r} is not an object")
        out.append(_spec(name, entry, base))
    return out


def _spec(name: str, entry: Dict[str, Any], base: str) -> ServerSpec:
    kind = str(entry.get("type") or entry.get("transport") or "").lower()
    env = {str(k): expand(str(v)) for k, v in (entry.get("env") or {}).items()}
    headers = {str(k): expand(str(v)) for k, v in (entry.get("headers") or {}).items()}
    if "url" in entry and kind not in ("stdio",):
        url = expand(str(entry["url"]))
        if kind == "sse":
            return ServerSpec(name=name, kind="http", url=url, headers=headers,
                              unsupported="legacy HTTP+SSE transport is not supported; use Streamable HTTP")
        return ServerSpec(name=name, kind="http", url=url, headers=headers)
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise ConfigError(f"server {name!r} has neither \"command\" nor \"url\"")
    args = [expand(str(a)) for a in (entry.get("args") or [])]
    cwd = entry.get("cwd")
    if isinstance(cwd, str):
        cwd = expand(cwd)
        if not os.path.isabs(cwd):
            cwd = os.path.join(base, cwd)
    return ServerSpec(name=name, kind="stdio", command=[expand(command)] + args, env=env,
                      cwd=cwd if isinstance(cwd, str) else None)


def load_args(path: str) -> Dict[str, Any]:
    """--args file: {"tool_name": {...arguments...}} or {"tool_name": [{...}, {...}]}."""
    data = _load_json(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected an object mapping tool names to arguments")
    for tool, value in data.items():
        items = value if isinstance(value, list) else [value]
        if not items or not all(isinstance(i, dict) for i in items):
            raise ConfigError(f"{path}: arguments for {tool!r} must be an object or a list of objects")
    return data

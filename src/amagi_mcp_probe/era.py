"""Find out which MCP protocol era a server speaks, and open a session in it.

Modern (2026-07-28 and later) is stateless: the client calls `server/discover`, and every
request carries the protocol version, client info and client capabilities in `params._meta`.
Legacy (2025-11-25 and earlier) is the `initialize` handshake plus `notifications/initialized`.

`connect()` asks `server/discover` first and falls back to `initialize`, following the
spec's backward-compatibility rules:

- a DiscoverResult (it lists supportedVersions), or a modern error (-32020, -32021, -32022),
  means the server is modern;
- any other JSON-RPC error, a timeout, or an HTTP 4xx without a JSON-RPC error body means
  it is legacy, so the client sends `initialize` instead.

The era is cached per server (command line, or URL) for the life of this process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .transport import (
    META_SERVER_INFO,
    HttpStatusError,
    RequestTimeout,
    RpcError,
    ServerExited,
    Transport,
    TransportError,
)

MODERN_VERSIONS = ["2026-07-28"]
LEGACY_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_MODERN = MODERN_VERSIONS[0]
LATEST_LEGACY = LEGACY_VERSIONS[0]

HEADER_MISMATCH = -32020
MISSING_CLIENT_CAPABILITY = -32021
UNSUPPORTED_VERSION = -32022
MODERN_ERRORS = {HEADER_MISMATCH, MISSING_CLIENT_CAPABILITY, UNSUPPORTED_VERSION}

DISCOVER_TIMEOUT = 10.0
ERAS = ("auto", "modern", "legacy")

# cache key -> era; see Transport.cache_key
_CACHE: Dict[str, str] = {}


def is_modern_version(version: Any) -> bool:
    """Protocol versions are dates; 2026-07-28 and later are modern."""
    return isinstance(version, str) and version >= LATEST_MODERN


def forget(key: Optional[str] = None) -> None:
    """Drop one cached era, or all of them."""
    if key is None:
        _CACHE.clear()
    else:
        _CACHE.pop(key, None)


def cached_era(key: str) -> Optional[str]:
    return _CACHE.get(key)


@dataclass
class Handshake:
    """What connect() negotiated, and how it got there."""

    era: str = "legacy"  # modern | legacy
    protocol_version: Optional[str] = None
    supported_versions: List[str] = field(default_factory=list)
    server_info: Dict[str, Any] = field(default_factory=dict)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    instructions: Optional[str] = None
    discover: Any = None  # raw server/discover result
    initialize: Any = None  # raw initialize result
    discover_error: Optional[BaseException] = None  # why server/discover gave no result
    fallback: Optional[str] = None  # why initialize was used
    discover_missing: bool = False  # modern server that does not implement server/discover
    discover_crashed: bool = False  # the server exited when it got server/discover
    from_cache: bool = False

    def label(self) -> str:
        """modern, legacy, or dual (modern, and its discover result also lists a legacy version)."""
        if self.era == "modern" and any(v in LEGACY_VERSIONS for v in self.supported_versions):
            return "dual"
        return self.era


class ConnectError(Exception):
    """No session could be opened. stage: start | discover | initialize."""

    def __init__(self, stage: str, message: str, cause: Optional[BaseException] = None,
                 handshake: Optional[Handshake] = None):
        self.stage = stage
        self.cause = cause
        self.handshake = handshake
        super().__init__(message)


def supported_from(data: Any) -> List[str]:
    """The `supported` list from an UnsupportedProtocolVersion (-32022) error's data."""
    if isinstance(data, dict) and isinstance(data.get("supported"), list):
        return [v for v in data["supported"] if isinstance(v, str)]
    return []


def _close(t: Optional[Transport]) -> None:
    if t is None:
        return
    try:
        t.close()
    except Exception:
        pass


def _start(factory: Callable[[], Transport], client_info: Dict[str, Any]) -> Transport:
    t = factory()
    t.client_info = dict(client_info)
    try:
        t.start()
    except TransportError as exc:
        raise ConnectError("start", str(exc), exc) from exc
    return t


def _apply_discover(hs: Handshake, result: Any) -> None:
    hs.discover = result
    if not isinstance(result, dict):
        return
    versions = result.get("supportedVersions")
    if isinstance(versions, list):
        hs.supported_versions = [v for v in versions if isinstance(v, str)]
    caps = result.get("capabilities")
    hs.capabilities = caps if isinstance(caps, dict) else {}
    if isinstance(result.get("instructions"), str):
        hs.instructions = result["instructions"]
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
    info = meta.get(META_SERVER_INFO)
    if not isinstance(info, dict):
        info = result.get("serverInfo")
    if isinstance(info, dict):
        hs.server_info = {k: info[k] for k in ("name", "title", "version") if k in info}


def _go_modern(t: Transport, hs: Handshake) -> None:
    ours = [v for v in MODERN_VERSIONS if v in hs.supported_versions]
    hs.era = "modern"
    hs.protocol_version = ours[0] if ours else LATEST_MODERN
    t.era = "modern"
    t.protocol_version = hs.protocol_version


def _initialize(t: Transport, hs: Handshake, version: str, timeout: float) -> None:
    params = {"protocolVersion": version, "capabilities": {}, "clientInfo": dict(t.client_info)}
    try:
        init = t.request("initialize", params, timeout=timeout, modern=False)
    except (TransportError, RpcError) as exc:
        if isinstance(exc, RequestTimeout):
            msg = f"no answer to initialize within {timeout:g}s"
        elif isinstance(exc, ServerExited):
            msg = f"{exc} during initialize"
        else:
            msg = str(exc)
        raise ConnectError("initialize", msg, exc, hs) from exc
    hs.era = "legacy"
    hs.initialize = init
    chosen = init.get("protocolVersion") if isinstance(init, dict) else None
    hs.protocol_version = chosen if isinstance(chosen, str) else None
    if isinstance(init, dict):
        caps = init.get("capabilities")
        hs.capabilities = caps if isinstance(caps, dict) else {}
        info = init.get("serverInfo")
        if isinstance(info, dict):
            hs.server_info = {k: info[k] for k in ("name", "title", "version") if k in info}
        if isinstance(init.get("instructions"), str):
            hs.instructions = init["instructions"]
    t.era = "legacy"
    t.protocol_version = hs.protocol_version or version
    try:
        t.notify("notifications/initialized")
    except TransportError as exc:
        raise ConnectError("initialize", f"could not send notifications/initialized: {exc}", exc, hs) from exc


def _discover(t: Transport, timeout: float) -> Any:
    return t.request("server/discover", {}, timeout=timeout, modern=True, version=LATEST_MODERN)


def _modern(t: Transport, hs: Handshake, timeout: float) -> None:
    """--era modern, or a server cached as modern: discover, no fallback."""
    try:
        _apply_discover(hs, _discover(t, timeout))
    except RpcError as exc:
        hs.discover_error = exc
        if exc.code in MODERN_ERRORS:
            raise ConnectError("discover", f"server/discover failed: {exc}", exc, hs) from exc
        hs.discover_missing = True  # a server that answers, but not server/discover
    except TransportError as exc:
        hs.discover_error = exc
        raise ConnectError("discover", f"server/discover failed: {exc}", exc, hs) from exc
    _go_modern(t, hs)


def _auto(factory: Callable[[], Transport], t: Transport, hs: Handshake, legacy_version: str,
          timeout: float, discover_timeout: float) -> Transport:
    reason: Optional[str] = None
    try:
        result = _discover(t, discover_timeout)
    except RpcError as exc:
        hs.discover_error = exc
        if exc.code == UNSUPPORTED_VERSION:
            supported = supported_from(exc.data)
            if any(v in MODERN_VERSIONS for v in supported):
                raise ConnectError("discover", f"server/discover rejected {LATEST_MODERN} but lists it: {exc}",
                                   exc, hs) from exc
            if supported and not any(v in LEGACY_VERSIONS for v in supported):
                raise ConnectError("discover", "server supports only " + ", ".join(supported)
                                   + "; this client speaks " + ", ".join(MODERN_VERSIONS + LEGACY_VERSIONS),
                                   exc, hs) from exc
            reason = f"server/discover: {exc}"
        elif exc.code in MODERN_ERRORS:
            hs.era = "modern"
            raise ConnectError("discover", f"server/discover failed: {exc}", exc, hs) from exc
        else:
            status = f" (HTTP {exc.http_status})" if exc.http_status else ""
            reason = f"server/discover answered error {exc.code}{status}"
    except RequestTimeout as exc:
        hs.discover_error = exc
        reason = f"no answer to server/discover within {discover_timeout:g}s"
    except ServerExited as exc:
        hs.discover_error = exc
        hs.discover_crashed = True
        reason = f"{exc} on server/discover; restarted it"
        _close(t)
        t = _start(factory, t.client_info)
    except HttpStatusError as exc:
        hs.discover_error = exc
        if not 400 <= exc.status < 500:
            raise ConnectError("discover", str(exc), exc, hs) from exc
        reason = f"server/discover got HTTP {exc.status} without a JSON-RPC error"
    except TransportError as exc:
        hs.discover_error = exc
        raise ConnectError("discover", str(exc), exc, hs) from exc
    else:
        _apply_discover(hs, result)
        legacy_only = (hs.supported_versions
                       and not any(is_modern_version(v) for v in hs.supported_versions)
                       and any(v in LEGACY_VERSIONS for v in hs.supported_versions))
        if not hs.supported_versions:
            # not a DiscoverResult: some legacy servers answer {} to any method they don't know.
            # If initialize fails too, the retry below goes modern.
            reason = "server/discover answered without supportedVersions"
        elif not legacy_only:
            _go_modern(t, hs)
            return t
        else:
            reason = "server/discover lists only legacy versions"

    hs.fallback = reason
    try:
        _initialize(t, hs, legacy_version, timeout)
        return t
    except ConnectError as err:
        cause = err.cause
        if isinstance(cause, RpcError) and (
            cause.code == UNSUPPORTED_VERSION and (any(is_modern_version(v) for v in supported_from(cause.data))
                                                    or not supported_from(cause.data))
        ):
            # initialize says "speak modern": the server was slow to answer discover, or ignored it
            pass
        elif not isinstance(cause, (RpcError, HttpStatusError)):
            raise
        try:
            result = _discover(t, timeout)
            _apply_discover(hs, result)
        except RpcError as exc2:
            if exc2.code in MODERN_ERRORS:
                raise err
            # rejects initialize and server/discover: a modern server without discover answers
            # a modern tools/list with a modern result (resultType, ttlMs)
            try:
                page = t.request("tools/list", {}, timeout=discover_timeout, modern=True, version=LATEST_MODERN)
            except (TransportError, RpcError):
                raise err
            if not isinstance(page, dict) or not ("resultType" in page or "ttlMs" in page):
                raise err
            hs.discover_missing = True
        except TransportError:
            raise err
        hs.fallback = None
        hs.initialize = None
        _go_modern(t, hs)
        return t


def connect(
    factory: Callable[[], Transport],
    client_info: Dict[str, Any],
    mode: str = "auto",
    legacy_version: str = LATEST_LEGACY,
    timeout: float = 30.0,
    discover_timeout: Optional[float] = None,
    use_cache: bool = True,
) -> Tuple[Transport, Handshake]:
    """Start the server from factory() and open a session in its era.

    mode: auto (discover, fall back to initialize), modern, or legacy. Returns the live
    transport and the Handshake. Raises ConnectError after closing the transport.
    """
    if mode not in ERAS:
        raise ValueError(f"unknown era {mode!r}; use one of {', '.join(ERAS)}")
    t = _start(factory, client_info)
    hs = Handshake()
    key = t.cache_key()
    era = mode
    if mode == "auto" and use_cache and key in _CACHE:
        era = _CACHE[key]
        hs.from_cache = True
    dt = discover_timeout if discover_timeout is not None else min(timeout, DISCOVER_TIMEOUT)
    try:
        if era == "legacy":
            _initialize(t, hs, legacy_version, timeout)
        elif era == "modern":
            _modern(t, hs, timeout)
        else:
            t = _auto(factory, t, hs, legacy_version, timeout, dt)
    except ConnectError as err:
        if err.handshake is None:
            err.handshake = hs
        _close(t)
        raise
    if use_cache:
        _CACHE[key] = hs.era
    return t, hs


__all__ = [
    "ConnectError",
    "Handshake",
    "LATEST_LEGACY",
    "LATEST_MODERN",
    "LEGACY_VERSIONS",
    "MODERN_ERRORS",
    "MODERN_VERSIONS",
    "UNSUPPORTED_VERSION",
    "cached_era",
    "connect",
    "forget",
    "is_modern_version",
    "supported_from",
]

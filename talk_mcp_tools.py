"""Extra MCP tools — allowlisted tools on one MCP server, advertised as Realtime functions.

The generic shape behind the fork's desk tools. An operator names ONE
streamable-HTTP MCP server, the env var that holds its bearer token, and an
explicit allowlist of tool names; each allowlisted tool the server lists is
advertised to the Realtime session as a function (its ``inputSchema`` is the
function's parameters) and a call lands here as a JSON-RPC ``tools/call``.

Three things this module is careful about:

- **Fail closed.** No server, no allowlist, or a server that cannot be listed
  within the timeout means NO extra tools — never "all of them".
- **The credential is spent on exactly one endpoint** — the configured
  server — as an ``Authorization: Bearer`` header. It is read by NAME
  (``TALK_EXTRA_MCP_KEY_ENV``) at call time, never cached, never logged,
  and never part of a tool result.
- **Bounded, speakable output.** A tool that fails RETURNS the failure as
  text; the transport never raises into the session.

Voice-callable by anyone in the room: a tool listed in
``TALK_EXTRA_MCP_TOOLS`` is treated as READ-ONLY by the Discord operator
gate. The server-side allowlist (what the key may call at all) is the
security boundary — put only read tools on the list.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from dataclasses import dataclass

import httpx

_log = logging.getLogger(__name__)

SERVER_ENV = "TALK_EXTRA_MCP_SERVER"
KEY_ENV_ENV = "TALK_EXTRA_MCP_KEY_ENV"
TOOLS_ENV = "TALK_EXTRA_MCP_TOOLS"
TIMEOUT_ENV = "TALK_EXTRA_MCP_TIMEOUT_S"

#: A live voice call cannot wait on a tool; the realtime model keeps talking
#: while a function is pending, but only for so long. Default and ceiling.
DEFAULT_TIMEOUT_S = 2.0
MAX_TIMEOUT_S = 10.0

#: How long one ``tools/list`` answer is reused for building schemas.
LIST_TTL_S = 300.0

#: Realtime function names: ``^[a-zA-Z0-9_-]{1,64}$`` (OpenAI). MCP names
#: like ``nt8.quote.get`` carry dots, so they are mapped, not passed through.
_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")
MAX_FUNCTION_NAME = 64
MAX_DESCRIPTION_CHARS = 1_000
MAX_RESULT_CHARS = 4_000


class MCPError(Exception):
    """A transport, protocol, or server-reported failure. Message is speakable."""


@dataclass(frozen=True)
class MCPEndpoint:
    """One MCP server: where, which env var holds its bearer, how long to wait."""

    url: str
    key_env: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S

    def headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.key_env:
            token = (os.environ.get(self.key_env) or "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers


def clamp_timeout(raw: str | float | None, default: float = DEFAULT_TIMEOUT_S) -> float:
    """A positive timeout no larger than :data:`MAX_TIMEOUT_S`; junk → default."""

    try:
        value = float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, MAX_TIMEOUT_S)


# -- transport ------------------------------------------------------------------

#: Test seam: a ``httpx.BaseTransport`` swapped in by the offline suite.
_transport: httpx.BaseTransport | None = None


def _decode_body(response: httpx.Response) -> dict:
    """The JSON-RPC envelope, whether the server answered JSON or an SSE frame."""

    text = response.text
    if "text/event-stream" in (response.headers.get("content-type") or ""):
        payloads = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if not payloads:
            raise MCPError("the MCP server sent an empty event stream")
        text = payloads[-1]
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise MCPError(f"the MCP server sent something that is not JSON-RPC: {exc}") from exc
    if not isinstance(body, dict):
        raise MCPError("the MCP server sent a non-object JSON-RPC body")
    return body


def call(endpoint: MCPEndpoint, method: str, params: dict | None = None) -> dict:
    """One JSON-RPC request; returns ``result``. Raises :class:`MCPError` only."""

    request = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 1_000_000, "method": method}
    if params is not None:
        request["params"] = params
    try:
        with httpx.Client(
            timeout=httpx.Timeout(endpoint.timeout_s), transport=_transport
        ) as client:
            response = client.post(
                endpoint.url, headers=endpoint.headers(), content=json.dumps(request)
            )
    except httpx.TimeoutException as exc:
        raise MCPError(f"the MCP server did not answer within {endpoint.timeout_s:g}s") from exc
    except httpx.HTTPError as exc:
        # httpx error text carries the URL, never the header, so it is safe to speak.
        raise MCPError(f"could not reach the MCP server: {type(exc).__name__}: {exc}") from exc
    if response.status_code >= 400:
        raise MCPError(f"the MCP server refused the request (HTTP {response.status_code})")
    body = _decode_body(response)
    if isinstance(body.get("error"), dict):
        err = body["error"]
        raise MCPError(f"the MCP server returned an error: {err.get('message') or err.get('code')}")
    result = body.get("result")
    if not isinstance(result, dict):
        raise MCPError("the MCP server answered without a result")
    return result


def list_tools(endpoint: MCPEndpoint) -> list[dict]:
    tools = call(endpoint, "tools/list", {}).get("tools")
    return [tool for tool in tools or [] if isinstance(tool, dict) and tool.get("name")]


def call_tool(endpoint: MCPEndpoint, name: str, arguments: dict | None = None) -> tuple[str, bool]:
    """``tools/call`` → ``(text, is_error)``. Text content blocks are joined."""

    result = call(endpoint, "tools/call", {"name": name, "arguments": arguments or {}})
    blocks = result.get("content") if isinstance(result.get("content"), list) else []
    text = "\n".join(
        str(block.get("text"))
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text") is not None
    )
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"])
    return text, bool(result.get("isError"))


# -- configuration (resolved at call time, never cached at import) --------------


def configured_endpoint() -> MCPEndpoint | None:
    url = (os.environ.get(SERVER_ENV) or "").strip()
    if not url:
        return None
    key_env = (os.environ.get(KEY_ENV_ENV) or "").strip() or None
    return MCPEndpoint(
        url=url, key_env=key_env, timeout_s=clamp_timeout(os.environ.get(TIMEOUT_ENV))
    )


def allowlist() -> tuple[str, ...]:
    raw = os.environ.get(TOOLS_ENV) or ""
    seen: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def function_name(mcp_name: str) -> str:
    """``nt8.quote.get`` → ``nt8_quote_get``; always a legal Realtime function name."""

    cleaned = _NAME_RE.sub("_", mcp_name).strip("_-") or "tool"
    return cleaned[:MAX_FUNCTION_NAME]


def _listed_matches(listed: str, wanted: str) -> bool:
    """A listed name matches the allowlist entry exactly, or after a gateway
    ``<server>-`` prefix (LiteLLM lists ``trading_stack-nt8.quote.get`` and
    calls ``nt8.quote.get``). Suffix match, so a prefix that itself contains
    ``-`` (``my-server-``) still matches. What gets CALLED is the entry as
    the operator wrote it: the full listed name on an exact match, the bare
    name on a prefix match."""

    return listed == wanted or listed.endswith("-" + wanted)


def _parameters(tool: dict) -> dict:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {"type": "object", "properties": {}, "additionalProperties": False}
    schema = copy.deepcopy(schema)
    schema.pop("$schema", None)
    schema.setdefault("properties", {})
    return schema


# -- registry --------------------------------------------------------------------

#: function name -> (allowlist entry to call, schema). Rebuilt by
#: :func:`extra_talk_tools`; consulted by :func:`handler_for`.
_registry: dict[str, tuple[str, dict]] = {}
_listing: tuple[tuple, float, list[dict]] = ((), 0.0, [])


def reset() -> None:
    """Forget the listing and registry (tests, and a config change)."""

    global _listing
    _registry.clear()
    _listing = ((), 0.0, [])


def _cached_listing(endpoint: MCPEndpoint, wanted: tuple[str, ...]) -> list[dict]:
    global _listing
    key = (endpoint.url, endpoint.key_env, wanted)
    cached_key, fetched_at, tools = _listing
    if cached_key == key and time.monotonic() - fetched_at < LIST_TTL_S:
        return tools
    tools = list_tools(endpoint)
    _listing = (key, time.monotonic(), tools)
    return tools


def extra_talk_tools(reserved: frozenset[str] = frozenset()) -> list[dict]:
    """Realtime function schemas for the allowlisted tools, or ``[]``.

    Empty when nothing is configured, when the allowlist is empty, and when
    the server cannot be listed in time — a session that starts without
    these tools beats a session that never starts. ``reserved`` names (the
    built-in talk tools) are never registered or advertised: an entry whose
    mapped function name collides with one is dropped with a warning, so a
    server tool can neither shadow a built-in schema nor borrow its
    classification at the operator gate.
    """

    endpoint = configured_endpoint()
    wanted = allowlist()
    if endpoint is None or not wanted:
        _registry.clear()
        return []
    try:
        listed = _cached_listing(endpoint, wanted)
    except MCPError as exc:
        _log.warning("extra MCP tools unavailable: %s", exc)
        _registry.clear()
        return []
    _registry.clear()
    schemas: list[dict] = []
    for entry in wanted:
        tool = next((t for t in listed if _listed_matches(str(t["name"]), entry)), None)
        if tool is None:
            _log.warning("extra MCP tool %r is allowlisted but the server does not list it", entry)
            continue
        name = function_name(entry)
        if name in reserved:
            _log.warning("extra MCP tool %r maps to the built-in name %r; dropped", entry, name)
            continue
        if name in _registry:
            continue
        schema = {
            "type": "function",
            "name": name,
            "description": str(tool.get("description") or entry)[:MAX_DESCRIPTION_CHARS],
            "parameters": _parameters(tool),
        }
        _registry[name] = (entry, schema)
        schemas.append(schema)
    return copy.deepcopy(schemas)


def is_extra_tool(name: str) -> bool:
    return name in _registry


def execute(name: str, arguments: dict | None) -> str:
    """Run one registered extra tool; failures come back as text, never raised."""

    entry = _registry.get(name)
    if entry is None:
        return f"{name} is not an available tool right now."
    endpoint = configured_endpoint()
    if endpoint is None:
        return f"{name} is no longer configured."
    try:
        text, is_error = call_tool(endpoint, entry[0], arguments or {})
    except MCPError as exc:
        return f"{name} failed: {exc}"
    if is_error:
        return f"{name} reported an error: {text or 'no detail'}"[:MAX_RESULT_CHARS]
    return (text or "(no output)")[:MAX_RESULT_CHARS]


def handler_for(name: str):
    """A ``talk_tools``-shaped handler for a registered extra tool, else ``None``."""

    if not is_extra_tool(name):
        return None
    return lambda arguments, _name=name: execute(_name, arguments)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "KEY_ENV_ENV",
    "MAX_TIMEOUT_S",
    "SERVER_ENV",
    "TOOLS_ENV",
    "MCPEndpoint",
    "MCPError",
    "allowlist",
    "call",
    "call_tool",
    "clamp_timeout",
    "configured_endpoint",
    "execute",
    "extra_talk_tools",
    "function_name",
    "handler_for",
    "is_extra_tool",
    "list_tools",
    "reset",
]

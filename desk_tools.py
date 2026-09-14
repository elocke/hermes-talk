"""Desk tools — five read-only market lookups the Realtime session can make directly.

A configuration PRESET on top of :mod:`talk_mcp_tools`: hand-written function
schemas the model can call by voice (``get_quote("NQ")``), each handler
resolving the front month first and then making one ``tools/call`` against
the trading MCP server, with the result compacted to a short JSON object the
model can read aloud — the number first, one line of context.

Why direct and not delegated: the realtime model has native async function
calling, so a lookup that takes under two seconds is conversationally free,
where a delegated agent run costs eight. Delegation stays for real work.

Boundary: the handlers use ONE bearer (``TALK_DESK_MCP_KEY``) against ONE
endpoint (``TALK_DESK_MCP_URL``). What that key may call is decided
server-side (the gateway's per-key tool allowlist) — the plugin adds nothing
a stolen key could not already do, and only calls read tools.

Enabled only by ``TALK_DESK_TOOLS=1``; every handler returns ``{"error": …}``
instead of raising. Timeouts default to two seconds.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
import re

try:
    from . import desk_front_month, talk_mcp_tools
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import desk_front_month
    import talk_mcp_tools

_log = logging.getLogger(__name__)

ENABLE_ENV = "TALK_DESK_TOOLS"
URL_ENV = "TALK_DESK_MCP_URL"
KEY_ENV = "TALK_DESK_MCP_KEY"
TIMEOUT_ENV = "TALK_DESK_MCP_TIMEOUT_S"

#: No default server: an unset ``TALK_DESK_MCP_URL`` means no call is ever
#: made (the fork's home is LiteLLM's per-server endpoint,
#: ``http://<litellm>/trading_stack/mcp`` — set it in the profile env).
DEFAULT_MCP_URL = ""

#: MCP tool names as the gateway accepts them on ``tools/call`` (bare).
MCP_QUOTE = "nt8.quote.get"
MCP_BARS = "nt8.bars.history"
MCP_GEX_SUMMARY = "gex.summary"
MCP_GEX_LEVELS = "gex.levels"
MCP_CHART_READ = "tv.chart.read"
MCP_SLOTS_LIST = "tv.slots.list"

#: Daily bar timeframe token for ``nt8.bars.history``.
DAILY_TIMEFRAME = "1D"
#: How many strikes ``get_levels`` reads out — a voice cannot carry a ladder.
MAX_LEVELS = 8

_EXPLICIT_CONTRACT = re.compile(r"^[A-Z0-9]{1,6} \d{2}-\d{2}$")
_GEX_SYMBOL = re.compile(r"^[A-Za-z0-9._-]{1,12}$")

_SYMBOL_PARAM = {
    "type": "string",
    "description": (
        "Futures root or alias — NQ, ES, MNQ, gold, crude — or an explicit NinjaTrader "
        "contract like 'NQ 12-26'. Single-name tickers (AAPL) are passed through."
    ),
}

_TOOL_GET_QUOTE: dict = {
    "type": "function",
    "name": "get_quote",
    "description": (
        "Latest last/bid/ask for a futures root or ticker from NinjaTrader. The front "
        "month is resolved for you (say 'NQ', not the contract), and a zero quote on "
        "the front month is retried on the next one. Say the last price first, then "
        "the contract it came from. An 'error' field means the desk could not answer "
        "— say so; never guess a price."
    ),
    "parameters": {
        "type": "object",
        "properties": {"symbol": _SYMBOL_PARAM},
        "required": ["symbol"],
        "additionalProperties": False,
    },
}

_TOOL_GET_GEX: dict = {
    "type": "function",
    "name": "get_gex",
    "description": (
        "Dealer gamma positioning for a symbol: regime (positive or negative gamma), "
        "the gamma flip, call wall and put wall in futures terms, and signed distances "
        "from the current futures price. Lead with the regime and the nearest wall. A "
        "null level is unknown, never 'at price'; ageSec says how old the snapshot is."
    ),
    "parameters": {
        "type": "object",
        "properties": {"symbol": _SYMBOL_PARAM},
        "required": ["symbol"],
        "additionalProperties": False,
    },
}

_TOOL_GET_LEVELS: dict = {
    "type": "function",
    "name": "get_levels",
    "description": (
        "The key gamma strikes for a symbol, in futures points, largest gamma first, "
        "with which wall each belongs to. Use when asked for levels to watch. Read out "
        "the walls and the two or three biggest strikes, not the whole ladder."
    ),
    "parameters": {
        "type": "object",
        "properties": {"symbol": _SYMBOL_PARAM},
        "required": ["symbol"],
        "additionalProperties": False,
    },
}

_TOOL_GET_SESSION_RANGE: dict = {
    "type": "function",
    "name": "get_session_range",
    "description": (
        "Today's session open, high, low and last from NinjaTrader daily bars for a "
        "futures root, with the prior session's high, low and close for reference. "
        "Front month resolved for you. Quote futures in points."
    ),
    "parameters": {
        "type": "object",
        "properties": {"symbol": _SYMBOL_PARAM},
        "required": ["symbol"],
        "additionalProperties": False,
    },
}

_TOOL_GET_CHART_READ: dict = {
    "type": "function",
    "name": "get_chart_read",
    "description": (
        "Read one of the desk's configured TradingView chart slots by name (nq-5m, "
        "mnq-1m …): its symbol, timeframe, last bar and loaded studies. Call with no "
        "slot to list the slot names and whether each one is live. A slot reported "
        "'unreachable' or 'absent' has no chart behind it right now — say so."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slot": {
                "type": "string",
                "description": "Slot name from the list, e.g. 'nq-5m'. Omit to list slots.",
            },
        },
        "additionalProperties": False,
    },
}

DESK_TOOL_SCHEMAS: tuple[dict, ...] = (
    _TOOL_GET_QUOTE,
    _TOOL_GET_GEX,
    _TOOL_GET_LEVELS,
    _TOOL_GET_SESSION_RANGE,
    _TOOL_GET_CHART_READ,
)


# -- configuration (resolved at call time, never cached at import) --------------


def enabled() -> bool:
    """``TALK_DESK_TOOLS`` truthy. Anything else, junk included, is off."""

    raw = (os.environ.get(ENABLE_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def configured() -> str | None:
    """Why the desk tools cannot run right now, or ``None`` when they can.

    Fail closed on all three: the flag, the server URL and the bearer. A
    handler is dispatchable whenever it is in ``_HANDLERS``, so the gate is
    here, not only at advertising time — no unauthenticated request ever
    leaves this module.
    """

    if not enabled():
        return f"desk tools are not enabled ({ENABLE_ENV} is off)"
    if not (os.environ.get(URL_ENV) or "").strip():
        return f"desk tools are not configured ({URL_ENV} is unset)"
    if not (os.environ.get(KEY_ENV) or "").strip():
        return f"desk tools are not configured ({KEY_ENV} is unset)"
    return None


def endpoint() -> talk_mcp_tools.MCPEndpoint:
    url = (os.environ.get(URL_ENV) or "").strip() or DEFAULT_MCP_URL
    return talk_mcp_tools.MCPEndpoint(
        url=url,
        key_env=KEY_ENV,
        timeout_s=talk_mcp_tools.clamp_timeout(os.environ.get(TIMEOUT_ENV)),
    )


def desk_talk_tools() -> list[dict]:
    """The five desk schemas, fresh copies per call, in the advertised order."""

    return copy.deepcopy(list(DESK_TOOL_SCHEMAS))


# -- helpers ---------------------------------------------------------------------


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _speak(payload: dict) -> str:
    return json.dumps(payload)


def _num(source: dict, *keys: str) -> float | None:
    """The first numeric value under any of ``keys``; bools and strings never count."""

    for key in keys:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _decode(text: str) -> dict | list:
    """A tool's text content as JSON; non-JSON text is wrapped, not dropped."""

    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text[:400]}


def _mcp(name: str, arguments: dict) -> dict | list:
    """One ``tools/call``; server-reported errors surface as ``{"error": …}``."""

    text, is_error = talk_mcp_tools.call_tool(endpoint(), name, arguments)
    payload = _decode(text) if text else {}
    if is_error or (isinstance(payload, dict) and payload.get("error") is not None):
        detail = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("code")
        return {"error": str(detail or text or f"{name} reported an error")}
    return payload


def _asof() -> dt.date | None:
    """The as-of date for front-month resolution; ``None`` means today. Test seam."""

    return None


def _instruments(symbol: str) -> tuple[list[str], dict | None]:
    """The NT8 instrument names to try, front month first, plus the resolution."""

    token = symbol.strip()
    if _EXPLICIT_CONTRACT.match(token.upper()):
        return [token.upper()], None
    root = desk_front_month.normalise_root(token)
    if root is None:
        return [token.upper()], None
    resolved = desk_front_month.resolve(root, _asof())
    names = [resolved["front"]]
    if resolved["next"]:
        names.append(resolved["next"])
    return names, resolved


def _gex_symbol(symbol: str) -> str | None:
    """The feed's symbol: a futures ROOT (never a contract), or a bare ticker."""

    token = symbol.strip()
    if _EXPLICIT_CONTRACT.match(token.upper()):
        token = token.split(" ")[0]
    root = desk_front_month.normalise_root(token)
    if root is not None:
        return root
    token = token.upper()
    return token if _GEX_SYMBOL.match(token) else None


def _guard(handler):
    """Every desk handler speaks its failure; nothing here raises into the session."""

    def wrapped(arguments: dict) -> str:
        refusal = configured()
        if refusal is not None:
            return _error(refusal)
        try:
            return handler(arguments or {})
        except talk_mcp_tools.MCPError as exc:
            return _error(str(exc))
        except Exception as exc:  # noqa: BLE001 — the model speaks the failure
            _log.warning("desk tool %s failed: %s: %s", handler.__name__, type(exc).__name__, exc)
            return _error(f"{type(exc).__name__}: {exc}")

    wrapped.__name__ = handler.__name__
    return wrapped


def _symbol_arg(arguments: dict) -> str:
    return str(arguments.get("symbol") or "").strip()


# -- handlers ----------------------------------------------------------------------


def _quote_fields(payload: dict) -> dict:
    return {
        "last": _num(payload, "last", "lastPrice", "price"),
        "bid": _num(payload, "bid", "bidPrice"),
        "ask": _num(payload, "ask", "askPrice"),
    }


@_guard
def _handle_get_quote(arguments: dict) -> str:
    symbol = _symbol_arg(arguments)
    if not symbol:
        return _error("get_quote needs a symbol")
    names, resolved = _instruments(symbol)
    tried: list[str] = []
    for instrument in names:
        payload = _mcp(MCP_QUOTE, {"instrument": instrument})
        if not isinstance(payload, dict):
            return _error(f"unexpected quote shape for {instrument}")
        if payload.get("error"):
            return _error(f"{instrument}: {payload['error']}")
        fields = _quote_fields(payload)
        tried.append(instrument)
        if any(value for value in fields.values()):
            spoken: dict = {**fields, "instrument": instrument}
            stamp = payload.get("time") or payload.get("timestamp") or payload.get("asof")
            if stamp is not None:
                spoken["time"] = stamp
            if resolved and resolved["roll_week"]:
                spoken["note"] = f"roll week — {resolved['front']} expires {resolved['expiry']}"
            return _speak(spoken)
    return _error(
        f"no quote (all zero) on {', '.join(tried)} — data not loaded or NT8 not connected"
    )


_GEX_SUMMARY_KEYS = (
    "futPrice",
    "regime",
    "flipFut",
    "callWallFut",
    "putWallFut",
    "distToFlip",
    "distToCallWall",
    "distToPutWall",
    "netGexM",
    "ratio",
    "ageSec",
)


@_guard
def _handle_get_gex(arguments: dict) -> str:
    symbol = _gex_symbol(_symbol_arg(arguments))
    if symbol is None:
        return _error("get_gex needs a symbol")
    payload = _mcp(MCP_GEX_SUMMARY, {"symbol": symbol})
    if not isinstance(payload, dict):
        return _error(f"unexpected gex shape for {symbol}")
    if payload.get("error"):
        return _error(f"{symbol}: {payload['error']}")
    spoken = {key: payload[key] for key in _GEX_SUMMARY_KEYS if key in payload}
    spoken["symbol"] = payload.get("symbol") or symbol
    return _speak(spoken)


@_guard
def _handle_get_levels(arguments: dict) -> str:
    symbol = _gex_symbol(_symbol_arg(arguments))
    if symbol is None:
        return _error("get_levels needs a symbol")
    payload = _mcp(MCP_GEX_LEVELS, {"symbol": symbol})
    if not isinstance(payload, dict):
        return _error(f"unexpected levels shape for {symbol}")
    if payload.get("error"):
        return _error(f"{symbol}: {payload['error']}")
    rows = [row for row in payload.get("levels") or [] if isinstance(row, dict)]
    rows.sort(key=lambda row: abs(_num(row, "gex") or 0), reverse=True)
    levels = []
    for row in rows[:MAX_LEVELS]:
        level = {"fut": _num(row, "fut"), "strike": _num(row, "strike"), "gex": _num(row, "gex")}
        if row.get("side"):
            level["side"] = row["side"]
        levels.append(level)
    spoken = {
        key: payload[key]
        for key in ("futPrice", "flipFut", "callWallFut", "putWallFut")
        if key in payload
    }
    spoken["symbol"] = payload.get("symbol") or symbol
    spoken["levels"] = levels
    return _speak(spoken)


def _bar(row: dict) -> dict:
    bar = {
        "open": _num(row, "open", "o"),
        "high": _num(row, "high", "h"),
        "low": _num(row, "low", "l"),
        "close": _num(row, "close", "c", "last"),
        "volume": _num(row, "volume", "v"),
    }
    stamp = row.get("time") or row.get("timestamp") or row.get("t")
    if stamp is not None:
        bar["time"] = stamp
    return bar


@_guard
def _handle_get_session_range(arguments: dict) -> str:
    symbol = _symbol_arg(arguments)
    if not symbol:
        return _error("get_session_range needs a symbol")
    names, _resolved = _instruments(symbol)
    rows: list[dict] = []
    tried: list[str] = []
    for instrument in names:
        args = {"instrument": instrument, "timeframe": DAILY_TIMEFRAME, "count": 2}
        payload = _mcp(MCP_BARS, args)
        if isinstance(payload, dict) and payload.get("error"):
            return _error(f"{instrument}: {payload['error']}")
        found = payload.get("bars") if isinstance(payload, dict) else payload
        rows = [row for row in found or [] if isinstance(row, dict)]
        tried.append(instrument)
        if rows:
            break
    if not rows:
        return _error(f"no daily bars on {', '.join(tried)} — data not loaded or NT8 not connected")
    today = _bar(rows[-1])
    spoken: dict = {
        "high": today["high"],
        "low": today["low"],
        "open": today["open"],
        "last": today["close"],
        "instrument": instrument,
    }
    if today.get("time") is not None:
        spoken["bar_time"] = today["time"]
    if len(rows) >= 2:
        prior = _bar(rows[-2])
        spoken["prior"] = {"high": prior["high"], "low": prior["low"], "close": prior["close"]}
    return _speak(spoken)


def _slots_summary(payload: dict) -> str:
    slots = []
    for slot in payload.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        entry = {"name": slot.get("name"), "state": slot.get("state")}
        for key in ("symbol", "timeframe"):
            if slot.get(key):
                entry[key] = slot[key]
        slots.append(entry)
    return _speak({"slots": slots})


def _matched_chart(readout: dict) -> dict | None:
    charts = [chart for chart in readout.get("charts") or [] if isinstance(chart, dict)]
    if not charts:
        return None
    index = readout.get("matchedIndex")
    if isinstance(index, int) and 0 <= index < len(charts):
        return charts[index]
    return next(
        (chart for chart in charts if chart.get("active") or chart.get("focused")), charts[0]
    )


@_guard
def _handle_get_chart_read(arguments: dict) -> str:
    slot = str(arguments.get("slot") or "").strip()
    if not slot:
        payload = _mcp(MCP_SLOTS_LIST, {})
        if not isinstance(payload, dict):
            return _error("unexpected slot list shape")
        if payload.get("error"):
            return _error(str(payload["error"]))
        return _slots_summary(payload)
    payload = _mcp(MCP_CHART_READ, {"slot": slot})
    if not isinstance(payload, dict):
        return _error(f"unexpected chart shape for {slot}")
    if payload.get("error"):
        return _error(f"{slot}: {payload['error']}")
    readout = payload.get("readout") if isinstance(payload.get("readout"), dict) else {}
    chart = _matched_chart(readout)
    if chart is None:
        return _error(f"{slot}: no chart behind that slot right now")
    spoken: dict = {"slot": payload.get("slot") or slot}
    for key in ("symbol", "timeframe"):
        if chart.get(key) is not None:
            spoken[key] = chart[key]
    last = chart.get("lastBar") or chart.get("last_bar") or chart.get("bar")
    if isinstance(last, dict):
        spoken["last_bar"] = _bar(last)
    studies = chart.get("studies")
    if isinstance(studies, list):
        spoken["studies"] = [
            study.get("name") if isinstance(study, dict) else str(study) for study in studies
        ][:12]
    if "cached" in payload:
        spoken["cached"] = bool(payload["cached"])
    return _speak(spoken)


DESK_HANDLERS: dict = {
    "get_quote": _handle_get_quote,
    "get_gex": _handle_get_gex,
    "get_levels": _handle_get_levels,
    "get_session_range": _handle_get_session_range,
    "get_chart_read": _handle_get_chart_read,
}

DESK_TOOL_NAMES = frozenset(DESK_HANDLERS)

__all__ = [
    "DEFAULT_MCP_URL",
    "DESK_HANDLERS",
    "DESK_TOOL_NAMES",
    "DESK_TOOL_SCHEMAS",
    "ENABLE_ENV",
    "KEY_ENV",
    "URL_ENV",
    "configured",
    "desk_talk_tools",
    "enabled",
    "endpoint",
]

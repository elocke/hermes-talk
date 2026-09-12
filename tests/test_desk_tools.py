"""Desk tools and the generic extra-MCP layer — offline, against recorded fixtures.

Fixtures under ``tests/fixtures/desk_*.json`` are JSON-RPC envelopes as the
LiteLLM MCP gateway returned them on 2026-09-12 (``desk_tools_list``,
``desk_gex_summary_nq``, ``desk_tv_slots_list``, ``desk_quote_bridge_down`` —
the last recorded while NinjaTrader was offline, which is exactly the error
path). The ``*_synth.json`` ones are SYNTHESIZED from the tools' published
descriptions because no live NT8 quote or bar was obtainable at recording
time; they pin the plugin's field tolerance, not the server's wire.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

import desk_front_month
import desk_tools
import talk_mcp_tools
import talk_operator_auth
import talk_tools

FIXTURES = Path(__file__).parent / "fixtures"
FUNCTION_NAME = r"^[a-zA-Z0-9_-]{1,64}$"
TOKEN = "desk-test-bearer-not-a-real-key"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Gateway:
    """A scripted MCP server: routes ``tools/call`` by (name, instrument) to fixtures."""

    def __init__(self, routes: dict, *, sse: bool = True, listing: str = "desk_tools_list"):
        self.routes = routes
        self.sse = sse
        self.listing = listing
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if body["method"] == "tools/list":
            payload = fixture(self.listing)
        else:
            params = body["params"]
            key = (
                params["name"],
                params["arguments"].get("instrument")
                or params["arguments"].get("symbol")
                or params["arguments"].get("slot"),
            )
            target = self.routes.get(key) or self.routes.get((params["name"], None))
            if target is None:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": f"unscripted {key}"},
                    },
                )
            if isinstance(target, Exception):
                raise target
            if isinstance(target, int):
                return httpx.Response(target, text="upstream says no")
            payload = fixture(target)
        payload = {**payload, "id": body["id"]}
        if self.sse:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"event: message\ndata: {json.dumps(payload)}\n\n",
            )
        return httpx.Response(200, json=payload)

    def calls(self) -> list[tuple[str, dict]]:
        out = []
        for request in self.requests:
            body = json.loads(request.content)
            if body["method"] == "tools/call":
                out.append((body["params"]["name"], body["params"]["arguments"]))
        return out


@pytest.fixture
def gateway(monkeypatch):
    def install(routes: dict, **kwargs) -> Gateway:
        gw = Gateway(routes, **kwargs)
        monkeypatch.setattr(talk_mcp_tools, "_transport", gw.transport())
        return gw

    return install


@pytest.fixture
def desk_env(monkeypatch):
    monkeypatch.setenv("TALK_DESK_TOOLS", "1")
    monkeypatch.setenv("TALK_DESK_MCP_KEY", TOKEN)
    # 2026-09-12 is a Saturday inside NQ's September roll week (roll Thu 09-17).
    monkeypatch.setattr(desk_tools, "_asof", lambda: dt.date(2026, 9, 12))


# -- advertising ---------------------------------------------------------------


def test_desk_tools_are_absent_by_default():
    names = {tool["name"] for tool in talk_tools.default_talk_tools()}
    assert names.isdisjoint(desk_tools.DESK_TOOL_NAMES)


@pytest.mark.parametrize("value", ["", "0", "false", "off", "junk"])
def test_anything_but_a_truthy_flag_leaves_desk_tools_off(monkeypatch, value):
    monkeypatch.setenv("TALK_DESK_TOOLS", value)
    assert not desk_tools.enabled()
    names = {tool["name"] for tool in talk_tools.default_talk_tools()}
    assert names.isdisjoint(desk_tools.DESK_TOOL_NAMES)


def test_desk_tools_are_appended_after_the_base_set_when_enabled(monkeypatch):
    base = [tool["name"] for tool in talk_tools.default_talk_tools()]
    monkeypatch.setenv("TALK_DESK_TOOLS", "1")
    names = [tool["name"] for tool in talk_tools.default_talk_tools()]
    assert names == [
        *base,
        "get_quote",
        "get_gex",
        "get_levels",
        "get_session_range",
        "get_chart_read",
    ]


def test_desk_schemas_are_valid_realtime_functions(desk_env):
    import re

    for tool in desk_tools.desk_talk_tools():
        assert tool["type"] == "function"
        assert re.match(FUNCTION_NAME, tool["name"])
        assert tool["description"].strip()
        params = tool["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        assert set(params.get("required", [])) <= set(params["properties"])


def test_desk_schemas_are_fresh_copies(desk_env):
    first = desk_tools.desk_talk_tools()
    first[0]["name"] = "mutated"
    assert desk_tools.desk_talk_tools()[0]["name"] == "get_quote"


def test_every_desk_tool_has_a_handler_and_is_classified_read_only():
    for name in desk_tools.DESK_TOOL_NAMES:
        assert name in talk_tools._HANDLERS
        assert name in talk_operator_auth.READ_ONLY_TALK_TOOLS
        assert name not in talk_operator_auth.MUTATING_TALK_TOOLS
    assert {tool["name"] for tool in desk_tools.DESK_TOOL_SCHEMAS} == desk_tools.DESK_TOOL_NAMES


def test_desk_tools_run_for_non_operators_in_discord(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.delenv("TALK_DISCORD_OPERATOR_USER_IDS", raising=False)
    for name in desk_tools.DESK_TOOL_NAMES:
        assert ledger.authorize_tool(name, {}) is None


# -- front month -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("root", "date", "front", "nxt", "roll_week"),
    [
        ("NQ", "2026-09-10", "NQ 09-26", "NQ 12-26", None),
        ("NQ", "2026-09-12", "NQ 09-26", "NQ 12-26", True),
        ("NQ", "2026-09-17", "NQ 09-26", "NQ 12-26", True),  # roll Thursday stays front
        ("NQ", "2026-09-18", "NQ 12-26", "NQ 03-27", False),
        ("MNQ", "2026-09-18", "MNQ 12-26", "MNQ 03-27", False),
        ("ES", "2026-12-16", "ES 12-26", "ES 03-27", True),
        ("ES", "2026-12-18", "ES 03-27", "ES 06-27", False),
        ("ES", "2026-01-02", "ES 03-26", "ES 06-26", False),
        ("MES", "2027-03-19", "MES 06-27", "MES 09-27", False),
        ("CL", "2026-09-12", "CL 10-26", "CL 11-26", False),
        ("CL", "2026-09-23", "CL 11-26", "CL 12-26", False),
        ("GC", "2026-09-12", "GC 10-26", "GC 12-26", False),
        ("GC", "2026-10-01", "GC 12-26", "GC 02-27", False),
        ("SI", "2026-09-12", "SI 12-26", "SI 03-27", False),
    ],
)
def test_front_month_on_known_dates(root, date, front, nxt, roll_week):
    resolved = desk_front_month.resolve(root, dt.date.fromisoformat(date))
    assert resolved["front"] == front
    assert resolved["next"] == nxt
    if roll_week is not None:
        assert resolved["roll_week"] is roll_week


def test_third_friday_anchors():
    assert desk_front_month.third_friday(2026, 9) == dt.date(2026, 9, 18)
    assert desk_front_month.third_friday(2026, 12) == dt.date(2026, 12, 18)


@pytest.mark.parametrize(
    ("token", "root"),
    [
        ("nq", "NQ"),
        ("NQ1!", "NQ"),
        ("nasdaq", "NQ"),
        ("MNQ", "MNQ"),
        ("spx", "ES"),
        ("es1!", "ES"),
        ("gold", "GC"),
        ("silver", "SI"),
        ("oil", "CL"),
        ("micro crude", "MCL"),
        ("ZZZ", None),
        ("AAPL", None),
    ],
)
def test_aliases_resolve_to_nt8_roots(token, root):
    assert desk_front_month.normalise_root(token) == root


def test_front_month_defaults_to_today():
    resolved = desk_front_month.resolve("NQ")
    assert resolved["asof"] == dt.datetime.now(dt.UTC).date().isoformat()
    assert resolved["front"].startswith("NQ ")


# -- get_quote -----------------------------------------------------------------


def test_get_quote_resolves_the_front_month_and_speaks_the_number_first(desk_env, gateway):
    gw = gateway({("nt8.quote.get", "NQ 09-26"): "desk_quote_nq_synth"})

    spoken = json.loads(talk_tools.execute_talk_tool("get_quote", {"symbol": "nasdaq"}))

    assert next(iter(spoken)) == "last"
    assert spoken["last"] == 29390.5
    assert spoken["instrument"] == "NQ 09-26"
    assert "roll week" in spoken["note"]
    assert gw.calls() == [("nt8.quote.get", {"instrument": "NQ 09-26"})]


def test_get_quote_spends_the_bearer_on_the_configured_endpoint_only(
    desk_env, gateway, monkeypatch
):
    monkeypatch.setenv("TALK_DESK_MCP_URL", "http://mcp.test/trading_stack/mcp")
    gw = gateway({("nt8.quote.get", "NQ 09-26"): "desk_quote_nq_synth"})

    spoken = talk_tools.execute_talk_tool("get_quote", {"symbol": "NQ"})

    request = gw.requests[0]
    assert str(request.url) == "http://mcp.test/trading_stack/mcp"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in spoken


def test_get_quote_retries_the_next_month_on_an_all_zero_quote(desk_env, gateway):
    gw = gateway(
        {
            ("nt8.quote.get", "NQ 09-26"): "desk_quote_zero_synth",
            ("nt8.quote.get", "NQ 12-26"): "desk_quote_nq_synth",
        }
    )

    spoken = json.loads(talk_tools.execute_talk_tool("get_quote", {"symbol": "NQ"}))

    assert spoken["instrument"] == "NQ 12-26"
    assert [call[1]["instrument"] for call in gw.calls()] == ["NQ 09-26", "NQ 12-26"]


def test_get_quote_all_zero_on_both_months_is_an_error_not_a_price(desk_env, gateway):
    gateway(
        {
            ("nt8.quote.get", "NQ 09-26"): "desk_quote_zero_synth",
            ("nt8.quote.get", "NQ 12-26"): "desk_quote_zero_synth",
        }
    )

    spoken = json.loads(talk_tools.execute_talk_tool("get_quote", {"symbol": "NQ"}))

    assert set(spoken) == {"error"}
    assert "NQ 09-26, NQ 12-26" in spoken["error"]


def test_get_quote_explicit_contract_is_passed_through(desk_env, gateway):
    gw = gateway({("nt8.quote.get", "NQ 12-26"): "desk_quote_nq_synth"})

    talk_tools.execute_talk_tool("get_quote", {"symbol": "nq 12-26"})

    assert gw.calls() == [("nt8.quote.get", {"instrument": "NQ 12-26"})]


def test_get_quote_bridge_down_is_spoken_as_an_error(desk_env, gateway):
    """Recorded on 2026-09-12 with NinjaTrader offline: isError with a
    BRIDGE_DOWN payload. The plugin must say the desk is unreachable, not
    read a zero as a price."""

    gateway({("nt8.quote.get", "NQ 09-26"): "desk_quote_bridge_down"})

    spoken = json.loads(talk_tools.execute_talk_tool("get_quote", {"symbol": "NQ"}))

    assert set(spoken) == {"error"}
    assert "bridge unreachable" in spoken["error"]


def test_get_quote_timeout_returns_an_error_and_never_raises(desk_env, gateway):
    gateway({("nt8.quote.get", "NQ 09-26"): httpx.ReadTimeout("slow")})

    spoken = json.loads(talk_tools.execute_talk_tool("get_quote", {"symbol": "NQ"}))

    assert set(spoken) == {"error"}
    assert "did not answer within 2s" in spoken["error"]


def test_get_quote_http_failure_returns_an_error(desk_env, gateway):
    gateway({("nt8.quote.get", "NQ 09-26"): 502})

    spoken = json.loads(talk_tools.execute_talk_tool("get_quote", {"symbol": "NQ"}))

    assert "HTTP 502" in spoken["error"]


def test_get_quote_unreachable_server_returns_an_error(desk_env, gateway):
    gateway({("nt8.quote.get", "NQ 09-26"): httpx.ConnectError("refused")})

    spoken = json.loads(talk_tools.execute_talk_tool("get_quote", {"symbol": "NQ"}))

    assert "could not reach" in spoken["error"]


def test_get_quote_without_a_symbol_asks_for_one(desk_env, gateway):
    gw = gateway({})
    assert json.loads(desk_tools.DESK_HANDLERS["get_quote"]({}))["error"].startswith(
        "get_quote needs"
    )
    assert gw.requests == []


# -- get_gex / get_levels ----------------------------------------------------------


def test_get_gex_reads_the_recorded_summary(desk_env, gateway):
    gw = gateway({("gex.summary", "NQ"): "desk_gex_summary_nq"})

    spoken = json.loads(talk_tools.execute_talk_tool("get_gex", {"symbol": "nq1!"}))

    assert next(iter(spoken)) == "futPrice"
    assert spoken["futPrice"] == 29391.75
    assert spoken["regime"] == "positive"
    assert spoken["flipFut"] is None  # unknown stays unknown, never zero
    assert spoken["symbol"] == "NQ"
    assert "view" not in spoken and "shift" not in spoken  # compacted for speech
    assert gw.calls() == [("gex.summary", {"symbol": "NQ"})]


def test_get_gex_passes_a_single_name_ticker_through(desk_env, gateway):
    gw = gateway({("gex.summary", "AAPL"): "desk_gex_summary_nq"})
    talk_tools.execute_talk_tool("get_gex", {"symbol": "aapl"})
    assert gw.calls() == [("gex.summary", {"symbol": "AAPL"})]


def test_get_gex_rejects_a_symbol_the_feed_would_reject(desk_env, gateway):
    gw = gateway({})
    spoken = json.loads(talk_tools.execute_talk_tool("get_gex", {"symbol": "not a symbol at all!"}))
    assert set(spoken) == {"error"}
    assert gw.requests == []


def test_get_gex_json_body_decodes_as_well_as_sse(desk_env, gateway):
    gateway({("gex.summary", "NQ"): "desk_gex_summary_nq"}, sse=False)
    spoken = json.loads(talk_tools.execute_talk_tool("get_gex", {"symbol": "NQ"}))
    assert spoken["futPrice"] == 29391.75


def test_get_levels_reads_out_the_biggest_strikes_first_and_bounded(desk_env, gateway):
    gateway({("gex.levels", "NQ"): "desk_gex_levels_nq_synth"})

    spoken = json.loads(talk_tools.execute_talk_tool("get_levels", {"symbol": "NQ"}))

    assert spoken["futPrice"] == 29391.75
    assert spoken["callWallFut"] == 29600.0
    assert len(spoken["levels"]) == desk_tools.MAX_LEVELS
    assert [level["fut"] for level in spoken["levels"][:2]] == [29600.0, 29100.0]
    assert spoken["levels"][0]["side"] == "call"
    assert "side" not in spoken["levels"][2]


# -- get_session_range ---------------------------------------------------------------


def test_get_session_range_uses_daily_bars_on_the_front_month(desk_env, gateway):
    gw = gateway({("nt8.bars.history", "NQ 09-26"): "desk_bars_nq_synth"})

    spoken = json.loads(talk_tools.execute_talk_tool("get_session_range", {"symbol": "NQ"}))

    assert list(spoken)[:2] == ["high", "low"]
    assert spoken["high"] == 29455.0
    assert spoken["last"] == 29390.5
    assert spoken["prior"] == {"high": 29388.5, "low": 29102.25, "close": 29315.75}
    assert gw.calls() == [
        ("nt8.bars.history", {"instrument": "NQ 09-26", "timeframe": "1D", "count": 2})
    ]


def test_get_session_range_bridge_down_is_an_error(desk_env, gateway):
    gateway({("nt8.bars.history", "NQ 09-26"): "desk_quote_bridge_down"})
    spoken = json.loads(talk_tools.execute_talk_tool("get_session_range", {"symbol": "NQ"}))
    assert set(spoken) == {"error"}


# -- get_chart_read ----------------------------------------------------------------


def test_get_chart_read_without_a_slot_lists_the_recorded_slots(desk_env, gateway):
    gw = gateway({("tv.slots.list", None): "desk_tv_slots_list"})

    spoken = json.loads(talk_tools.execute_talk_tool("get_chart_read", {}))

    assert spoken["slots"][0] == {
        "name": "nq-1h",
        "state": "unreachable",
        "symbol": "NQ1!",
        "timeframe": "60",
    }
    assert all("reason" not in slot for slot in spoken["slots"])
    assert gw.calls() == [("tv.slots.list", {})]


def test_get_chart_read_reads_the_matched_chart_of_a_slot(desk_env, gateway):
    gw = gateway({("tv.chart.read", "nq-5m"): "desk_chart_read_synth"})

    spoken = json.loads(talk_tools.execute_talk_tool("get_chart_read", {"slot": "nq-5m"}))

    assert spoken["slot"] == "nq-5m"
    assert spoken["symbol"] == "NQ1!"  # matchedIndex 1, not the focused pane
    assert spoken["last_bar"]["close"] == 29390.5
    assert spoken["studies"] == ["VWAP", "EMA 21", "Volume"]
    assert spoken["cached"] is False
    assert gw.calls() == [("tv.chart.read", {"slot": "nq-5m"})]


# -- configuration -------------------------------------------------------------------


def test_desk_endpoint_defaults_to_the_trading_stack_gateway(monkeypatch):
    endpoint = desk_tools.endpoint()
    assert endpoint.url == desk_tools.DEFAULT_MCP_URL
    assert endpoint.key_env == "TALK_DESK_MCP_KEY"
    assert endpoint.timeout_s == 2.0


@pytest.mark.parametrize(
    ("raw", "expected"), [("0.5", 0.5), ("60", 10.0), ("junk", 2.0), ("-1", 2.0), ("", 2.0)]
)
def test_desk_timeout_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("TALK_DESK_MCP_TIMEOUT_S", raw)
    assert desk_tools.endpoint().timeout_s == expected


def test_a_missing_key_sends_no_authorization_header(monkeypatch, gateway):
    monkeypatch.delenv("TALK_DESK_MCP_KEY", raising=False)
    gw = gateway({("gex.summary", "NQ"): "desk_gex_summary_nq"})
    talk_tools.execute_talk_tool("get_gex", {"symbol": "NQ"})
    assert "authorization" not in gw.requests[0].headers


# -- the generic layer: TALK_EXTRA_MCP_SERVER ----------------------------------------


@pytest.fixture
def extra_env(monkeypatch):
    monkeypatch.setenv("TALK_EXTRA_MCP_SERVER", "http://mcp.test/trading_stack/mcp")
    monkeypatch.setenv("TALK_EXTRA_MCP_KEY_ENV", "MY_MCP_TOKEN")
    monkeypatch.setenv("MY_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("TALK_EXTRA_MCP_TOOLS", "nt8.quote.get, gex.summary,nt8.quote.get")


def test_extra_tools_are_absent_without_a_server(gateway):
    gw = gateway({})
    assert talk_mcp_tools.extra_talk_tools() == []
    assert gw.requests == []


def test_extra_tools_are_absent_with_an_empty_allowlist(monkeypatch, gateway):
    """No allowlist means NO tools, never all of them — and the server is not even asked."""

    monkeypatch.setenv("TALK_EXTRA_MCP_SERVER", "http://mcp.test/mcp")
    gw = gateway({})
    assert talk_mcp_tools.extra_talk_tools() == []
    assert gw.requests == []


def test_extra_tools_are_built_from_the_recorded_listing(extra_env, gateway):
    gw = gateway({})

    tools = talk_mcp_tools.extra_talk_tools()

    assert [tool["name"] for tool in tools] == ["nt8_quote_get", "gex_summary"]
    quote = tools[0]
    assert quote["type"] == "function"
    assert quote["parameters"]["required"] == ["instrument"]
    assert "$schema" not in quote["parameters"]
    assert len(tools[1]["description"]) <= talk_mcp_tools.MAX_DESCRIPTION_CHARS
    assert json.loads(gw.requests[0].content)["method"] == "tools/list"
    assert gw.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_extra_tools_ride_default_talk_tools(extra_env, gateway):
    gateway({})
    names = [tool["name"] for tool in talk_tools.default_talk_tools()]
    assert names[-2:] == ["nt8_quote_get", "gex_summary"]


def test_an_allowlisted_name_the_server_does_not_list_is_skipped(monkeypatch, extra_env, gateway):
    monkeypatch.setenv("TALK_EXTRA_MCP_TOOLS", "nt8.order.place,gex.summary")
    gateway({})
    assert [tool["name"] for tool in talk_mcp_tools.extra_talk_tools()] == ["gex_summary"]
    assert not talk_mcp_tools.is_extra_tool("nt8_order_place")


def test_extra_tool_calls_the_bare_name_with_the_bearer(extra_env, gateway):
    gw = gateway({("gex.summary", "NQ"): "desk_gex_summary_nq"})
    talk_mcp_tools.extra_talk_tools()

    spoken = talk_tools.execute_talk_tool("gex_summary", {"symbol": "NQ"})

    assert '"futPrice":29391.75' in spoken
    assert gw.calls() == [("gex.summary", {"symbol": "NQ"})]
    assert gw.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in spoken


def test_extra_tool_server_error_is_spoken_not_raised(extra_env, gateway):
    gateway({("nt8.quote.get", "NQ 09-26"): "desk_quote_bridge_down"})
    talk_mcp_tools.extra_talk_tools()

    spoken = talk_tools.execute_talk_tool("nt8_quote_get", {"instrument": "NQ 09-26"})

    assert spoken.startswith("nt8_quote_get reported an error:")
    assert "BRIDGE_DOWN" in spoken


def test_extra_tool_timeout_is_spoken_not_raised(extra_env, gateway):
    gateway({("gex.summary", "NQ"): httpx.ReadTimeout("slow")})
    talk_mcp_tools.extra_talk_tools()
    assert "did not answer within 2s" in talk_tools.execute_talk_tool(
        "gex_summary", {"symbol": "NQ"}
    )


def test_extra_tools_degrade_to_none_when_the_listing_fails(extra_env, monkeypatch):
    def boom(request):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(talk_mcp_tools, "_transport", httpx.MockTransport(boom))

    assert talk_mcp_tools.extra_talk_tools() == []
    assert not talk_mcp_tools.is_extra_tool("gex_summary")
    # The base session still mints.
    assert "search_memory" in {tool["name"] for tool in talk_tools.default_talk_tools()}


def test_an_unregistered_name_is_still_a_client_bug(extra_env, gateway):
    gateway({})
    talk_mcp_tools.extra_talk_tools()
    with pytest.raises(talk_tools.TalkToolError):
        talk_tools.execute_talk_tool("nt8_order_place", {})


def test_registered_extra_tools_pass_the_discord_read_only_gate(extra_env, gateway, monkeypatch):
    gateway({})
    talk_mcp_tools.extra_talk_tools()
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.delenv("TALK_DISCORD_OPERATOR_USER_IDS", raising=False)

    assert ledger.authorize_tool("gex_summary", {}) is None
    assert "not run" in ledger.authorize_tool("nt8_order_place", {})


def test_reset_forgets_the_registry(extra_env, gateway):
    gateway({})
    talk_mcp_tools.extra_talk_tools()
    assert talk_mcp_tools.is_extra_tool("gex_summary")
    talk_mcp_tools.reset()
    assert not talk_mcp_tools.is_extra_tool("gex_summary")


@pytest.mark.parametrize(
    ("mcp", "fn"),
    [
        ("nt8.quote.get", "nt8_quote_get"),
        ("gex.summary", "gex_summary"),
        ("a b/c", "a_b_c"),
        ("...", "tool"),
        ("x" * 90, "x" * 64),
    ],
)
def test_function_names_are_legal_for_realtime(mcp, fn):
    import re

    assert talk_mcp_tools.function_name(mcp) == fn
    assert re.match(FUNCTION_NAME, fn)


def test_listed_names_match_bare_allowlist_entries_through_a_gateway_prefix():
    assert talk_mcp_tools._listed_matches("trading_stack-nt8.quote.get", "nt8.quote.get")
    assert talk_mcp_tools._listed_matches("nt8.quote.get", "nt8.quote.get")
    assert not talk_mcp_tools._listed_matches("trading_stack-nt8.quote.get", "quote.get")

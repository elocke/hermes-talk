# PR draft — upstream TheSmokeDev/hermes-talk

**Title:** feat(tools): expose allowlisted MCP tools as realtime functions (`TALK_EXTRA_MCP_SERVER`)

## Summary

Adds one generic, fail-closed extension point to the talk tool surface: an
operator names ONE streamable-HTTP MCP server, the env var that holds its
bearer, and an explicit allowlist of tool names; each allowlisted tool the
server lists is advertised to the Realtime session as a function (its
`inputSchema` becomes the function's parameters) and a call lands as a
JSON-RPC `tools/call`. Nothing else in the plugin changes shape.

Also carried in this branch (fork-only, may be dropped from the upstream PR or
kept as a documented preset): `desk_tools.py`, five hand-written read-only
market lookups on the same transport with calendar front-month resolution.

## Motivation

`gpt-realtime-2.1` has native async function calling: the model keeps
talking while a function is pending and only commits to the number when the
result lands. A direct read that answers in 0.5–2 s is therefore
conversationally free. Today the only way to reach a host tool from a Talk
session is `delegate_task`, which costs a full agent turn (~8 s measured
through a gateway) and, on Discord, is gated to configured operators — so a
guild member cannot ask "what's NQ at?" at all. Reads belong in the session;
delegation stays for real work.

The two alternatives were rejected on blast radius: OpenAI-native remote MCP
(`tools: [{type: "mcp", …}]`) requires publishing the MCP endpoint to the
internet, ships the bearer inside a session payload to OpenAI, gates
first-turn availability on `mcp_list_tools`, and is undocumented on the
ChatGPT-OAuth realtime entitlement. Exposing the host's whole tool catalog
(the "host execution attachment" lane) depends on an upstream hermes-agent
change that has not landed and is default-deny anyway.

## Design

- `talk_mcp_tools.py` (new): `MCPEndpoint`, `call()` (JSON-RPC over POST,
  JSON or SSE answers), `list_tools()`, `call_tool()`, and a registry built by
  `extra_talk_tools()` from `tools/list` ∩ allowlist. MCP names are mapped to
  legal Realtime function names (`nt8.quote.get` → `nt8_quote_get`); the
  allowlist entry is what gets called, matched against listed names exactly or
  after a gateway `<server>-` prefix (LiteLLM lists `trading_stack-nt8.quote.get`
  and calls `nt8.quote.get`). Listing is cached 300 s per config tuple.
- `talk_tools.py`: `default_talk_tools()` appends `extra_talk_tools()` (and the
  desk preset when `TALK_DESK_TOOLS=1`), each in its own `try` so an
  unreachable server costs only those tools, never the mint;
  `execute_talk_tool()` falls back to `talk_mcp_tools.handler_for(name)` for
  registered names — an unregistered name is still `TalkToolError`.
- `talk_operator_auth.py`: `authorize_tool()` treats a REGISTERED extra tool
  as read-only — **by the operator's declaration**, not by inspection: the
  plugin cannot know what a server tool does. The check runs AFTER the
  built-in `MUTATING_TALK_TOOLS` denial, and `extra_talk_tools(reserved=…)`
  refuses to register any entry whose mapped function name collides with a
  built-in (`delegate.task` → `delegate_task` is dropped with a warning), so
  a server tool can neither shadow a built-in schema nor borrow its
  classification. The five desk names are added to `READ_ONLY_TALK_TOOLS`
  statically.
- Proposed upstream default: `TALK_EXTRA_MCP_READ_ONLY` (default `true`)
  naming the declaration explicitly; with it `false`, extra tools would fall
  into the operator-only path like `MUTATING_TALK_TOOLS`. Not implemented
  here — maintainers may prefer the declaration to be per-tool.
- Every knob is resolved at call time (no import-time caching); the bearer is
  read by NAME, sent only to the configured server, never logged, and never
  part of a result. No new dependency (`httpx` is already a runtime dep).
- Desk handlers are dispatchable whenever they are in `_HANDLERS`, so they
  gate themselves at call time: flag off, URL unset or bearer unset → spoken
  `{"error": …}` and NO request (there is no default URL; an unauthenticated
  request never leaves the plugin).
- Desk preset (`desk_tools.py` + `desk_front_month.py`): schemas match the
  `_TOOL_*` dict shape; handlers return compact JSON (number first) and
  `{"error": …}` on any failure; front month by CME calendar rules (quarterly
  HMUZ, equity-index roll = Thursday before 3rd Friday; metals/energy monthly),
  zero quote on the front month retried on the next.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `TALK_EXTRA_MCP_SERVER` | unset | MCP server URL; unset = off |
| `TALK_EXTRA_MCP_KEY_ENV` | unset | NAME of the env var holding the bearer |
| `TALK_EXTRA_MCP_TOOLS` | unset | comma allowlist of MCP tool names; empty = no tools |
| `TALK_EXTRA_MCP_TIMEOUT_S` | `2` | per-request timeout, capped at 10 |
| `TALK_DESK_TOOLS` | unset | `1` advertises the five desk tools |
| `TALK_DESK_MCP_URL` / `TALK_DESK_MCP_KEY` / `TALK_DESK_MCP_TIMEOUT_S` | see manual | the desk preset's server, bearer, timeout |

Manual rows: `docs/OPERATING.md` "Extra MCP tools" / "Desk tools"; card
rows in `docs/VOICE-COMMANDS.md`; README Knobs; CHANGELOG Unreleased.

## Security note

The plugin is not the boundary and does not try to be. The bearer's reach is
decided server-side — the gateway's per-key tool allowlist — so the plugin
adds nothing a stolen key could not already do, and the operator's
`TALK_EXTRA_MCP_TOOLS` list can only NARROW that. Fail-closed at every step:
no allowlist means no tools (never all), an unlistable server means none, an
unregistered name is a client bug, a timeout is spoken. Allowlisted tools are
voice-callable by anyone in a Discord room; the manual says so and tells the
operator to list read tools only. Nothing here writes a credential store.

## Tests

`tests/test_desk_tools.py`, offline against `tests/fixtures/desk_*.json`
(recorded JSON-RPC envelopes from a LiteLLM MCP gateway: `tools/list`,
`gex.summary`, `tv.slots.list`, and a `nt8.quote.get` recorded with
NinjaTrader offline as the error path; `*_synth.json` are synthesized from
the tools' published descriptions and say so). Covers: absent by default and
on junk flags; advertised order; schema validity (Realtime name regex,
object parameters, required ⊆ properties); handler ↔ classification
mapping; Discord gate for non-operators; front-month resolution on 14 known
dates plus aliases; front-month fallback on a zero quote; bearer sent only to
the configured URL and never in output; BRIDGE_DOWN, timeout, HTTP 5xx and
connect failures all return `{"error"}` and never raise; SSE and JSON bodies;
the generic layer's listing, allowlist filtering, prefix matching, name
sanitization, degradation on a failed listing, dispatch through
`execute_talk_tool`, and the read-only gate. Existing suites untouched except
`test_no_handler_is_orphaned` (desk names added as conditional, the same
pattern as `search_vault`) and an autouse conftest fixture that clears the
new env so the advertised list never depends on the box.

`uv run --extra dev pytest -q` → 2478 passed, 61 skipped, 5 xfailed;
`uv run --extra dev ruff check .` → clean.

## Follow-ups

- `hermes talk check` live receipt on a real install (not run here — the
  branch is not deployed).
- `talk doctor` rows for the new knobs (server reachable? allowlist entries
  all listed?) — the doctor is read-only and would fit.
- `.env.example` rows (not edited in this branch: the authoring environment
  blocks writes to `.env*` paths).
- Optional: per-tool description override (`TALK_EXTRA_MCP_DESCRIBE_<name>`)
  for servers whose descriptions are written for agents, not voices.
- Consider moving the five desk tools out of the plugin into a preset file
  the operator points at, once the generic layer is upstream.

# Running this fork — trading profile only

Branch `feat/desk-tools-v0.22.0`: the desk-tools commits rebased onto upstream
v0.22.0 (`7ad833e`). The older `feat/desk-tools` (on v0.19.0) is kept for rollback.

## How it is deployed

Only the `trading` profile on LXC 213 runs this fork; every other profile runs
upstream. The plugin is a git clone on the hermes-home volume
(`/opt/data/profiles/trading/plugins/hermes-talk`, origin `elocke/hermes-talk`).

It is NOT moved with `hermes plugins install/update`: in that deployment both
fail for hermes-talk (one uv workspace with several members named `hermes-talk`,
NousResearch/hermes-agent#125252). homelab `tofu/lxc/hermes/scripts/update-plugins.py`
moves it instead — `GIT_PINS` maps this fork's origin to the commit to run, and
the script does `git fetch` + detached checkout, leaving `.env` untouched. Bump
that pin by PR, run the script with `--apply`, then restart the root gateway.

## Profile env (values never in the repo)

In the profile's `/opt/data/profiles/trading/.env` (the plugin's own `.env`
holds the other `TALK_*` settings):

```
TALK_DESK_TOOLS=1
TALK_DESK_MCP_URL=http://192.168.2.210:4000/trading_stack/mcp
TALK_DESK_MCP_KEY=<the trading profile's LiteLLM key>
```

All three are required: the handlers refuse (spoken `{"error": …}`, no request)
when any one is missing. Never set `TALK_DESK_TOOLS=1` container-wide — it would
advertise the desk tools to every profile.

## Rebasing onto a new upstream release

```bash
git fetch upstream --tags
git checkout -b feat/desk-tools-vX.Y.Z feat/desk-tools-v0.22.0
git rebase vX.Y.Z            # CHANGELOG: keep upstream, re-add the [Unreleased] entry
pip install -e '.[dev]' -r requirements-lint.txt && pytest -q && ruff check .
git push origin feat/desk-tools-vX.Y.Z   # new branch: never force-push a pinned one
```

Then smoke the desk handlers in the container against the live trading MCP
server, bump `GIT_PINS`, apply, restart, and check `hermes -p trading talk doctor`.

## Rollback

Run the script with the previous commit in `GIT_PINS` (it prints the old commit
on every move), or `git checkout --detach <old>` in the plugin dir; restart the
root gateway.

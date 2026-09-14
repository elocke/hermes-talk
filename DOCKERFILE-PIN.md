# Pinning this fork into the hermes image — NOT deployed

Feature commit: `10eaf22d9dae76824ad4cb4197442e93beb7704a` (branch `feat/desk-tools`,
on upstream `ab2691c` = v0.19.0). The branch tip is this file on top of that commit;
pin the tip sha once the lead pushes the fork (`git rev-parse feat/desk-tools`).

## How the plugin is installed today (read-only evidence, 2026-09-12)

`/mnt/config/hermes/data/profiles/trading/plugins/.install-metadata.json` on LXC 213:

```json
{ "hermes-talk": { "pinned": false,
                   "revision": "e814b1148befb256b1a711d4ee3d1783f5ca28f8",
                   "source": "https://github.com/TheSmokeDev/hermes-talk.git" } }
```

So the install is **per-profile on the hermes-home volume**, made by
`hermes -p trading plugins install …` — the image (`homelab/tofu/lxc/hermes/dockerfile/Dockerfile`)
does not carry the plugin at all. The running revision is v0.17.0; this branch targets
main (v0.19.0). Tool-registry surface delta e814b11 → ab2691c: `default_talk_tools()`,
`_HANDLERS`, `READ_ONLY_TALK_TOOLS` / `MUTATING_TALK_TOOLS` and `execute_talk_tool()` are
the same shape in both; v0.18/0.19 added the GPT-Live lane and dashboard task handoffs,
which this branch does not touch. Installing the fork therefore also moves `trading`
from 0.17.0 to 0.19.0+ — read CHANGELOG 0.18.0/0.19.0 before the gated install.

## 1. Image-side pin (config-as-code; the source the profile install reads)

Add to `Dockerfile` after the `RUN ln -sf /opt/hermes/bin/hermes …` line and before
`USER hermes` (root is still active there; `git` is in the base image because
`hermes plugins install` clones with it):

```dockerfile
# hermes-talk fork with desk tools (elocke/hermes-talk feat/desk-tools). Staged in
# the image so the per-profile install below is a local, pinned clone — the volume
# install is the one Hermes actually loads (plugins live per profile on hermes-home).
# Fetch BY SHA: a depth-1 clone of a branch cannot check out an arbitrary commit.
# renovate: ignore — pinned by hand, bump with the fork.
ARG HERMES_TALK_FORK=https://github.com/elocke/hermes-talk.git
ARG HERMES_TALK_SHA=10eaf22d9dae76824ad4cb4197442e93beb7704a
RUN git init -q /opt/hermes-talk-dist \
 && git -C /opt/hermes-talk-dist fetch -q --depth 1 "$HERMES_TALK_FORK" "$HERMES_TALK_SHA" \
 && git -C /opt/hermes-talk-dist checkout -q FETCH_HEAD \
 && chown -R 10000:10000 /opt/hermes-talk-dist
```

Verification after the (gated) image build: `docker exec hermes git -C /opt/hermes-talk-dist rev-parse HEAD`
→ `10eaf22d9dae76824ad4cb4197442e93beb7704a`.

## 2. Per-profile install on the volume (GATED — writes a live volume)

The form CONTRIBUTING.md documents for running a branch live, scoped to the profile:

```bash
# lead pushes the fork first (never from a subagent):  git push elocke feat/desk-tools
ssh hermes-host docker exec hermes hermes -p trading plugins install --force \
    --ref 10eaf22d9dae76824ad4cb4197442e93beb7704a elocke/hermes-talk --enable
# then restart the trading gateway — a running gateway keeps the old code until it restarts
```

Verification: `ssh hermes-host cat /mnt/config/hermes/data/profiles/trading/plugins/.install-metadata.json`
→ `"revision": "10eaf22…"`, `"source": "https://github.com/elocke/hermes-talk.git"`; then
`hermes -p trading talk doctor` still green and `talk_status` reports version `0.19.0`.

If the image stage from §1 is present, the same command with the local path as the
identifier (`/opt/hermes-talk-dist`) avoids a network clone from inside the container —
untested here; the `--ref <sha> owner/repo` form above is the documented one.

## 3. Profile env (operator items — values never in the repo)

In `/opt/data/profiles/trading/.env` on the volume (loaded by the gateway with
`override=True` before config parse, per hermes.yml):

```
TALK_DESK_TOOLS=1
TALK_DESK_MCP_URL=http://192.168.2.210:4000/trading_stack/mcp
TALK_DESK_MCP_KEY=<the trading profile's LiteLLM key — same value as LITELLM_KEY in that .env>
```

All three are required: the handlers refuse (spoken `{"error": …}`, no request) when any
one is missing, and there is no built-in default URL.
Do NOT put `TALK_DESK_TOOLS=1` in hermes.yml: it is container-wide and would advertise
the desk tools to every profile's Talk session, not just `trading`.

## 4. Rollback

`hermes -p trading plugins install --force --ref e814b1148befb256b1a711d4ee3d1783f5ca28f8 TheSmokeDev/hermes-talk --enable`
+ gateway restart; remove the two `.env` lines. The image stage is inert without the
profile install and needs no rollback.

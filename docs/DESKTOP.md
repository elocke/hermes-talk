# Hermes Talk in Desktop

The Desktop integration adds **Talk** beside the chat composer. It opens the
same task picker, transcript and controls used by the dashboard, inside Desktop.
The generated entrypoint is `desktop/plugin.js`; it does not embed a second
provider implementation or an external browser page.

## Required host support

This candidate requires a Hermes Desktop host with both:

- `useComposerVoiceController()` reporting `microphoneLease: 1` and
  `pinnedRest: 1`, and granting an abortable microphone lease;
- plugin REST accepting an explicit `{connectionId, profile}` scope and a
  `pluginToken` forwarded only as `X-Hermes-Plugin-Token` in its plugin namespace.

The companion host change builds on the composer ownership controller proposed
in [Hermes PR #100666](https://github.com/NousResearch/hermes-agent/pull/100666).
That proposal by itself does not supply the complete contract above. On an older
host, Talk displays an update-required message before requesting credentials or
microphone access. This page does not imply those changes are in stock Desktop.

## Open Talk

1. Install the matching host build and this plugin candidate in the Hermes home
   used by Desktop. Preserve the old host revision and plugin directory for rollback.
2. Restart Desktop so it loads the new renderer SDK and plugin entrypoint.
3. In Desktop **Capabilities → Plugins**, enable **Hermes Talk** if its Desktop
   contribution is disabled. Installed agent packages are opt-in on Desktop.
4. Open a connected Hermes conversation, then click **Talk** beside its composer.
5. If Talk asks for `TALK_DASHBOARD_TOKEN`, enter the token configured on the selected
   host and click **Use token**. The task list refreshes after authentication.
   This is the Talk access token, not a provider API key.
6. Select the authorized Hermes task in the Talk window and click **Start**.
7. Allow the requested microphone access. Click **Stop**, or close the Talk window,
   to end audio. Accepted background work continues in its owning task.

The plugin installer installs the repository, including `desktop/plugin.js`.
Installing the Python wheel alone does not register a Desktop contribution.
The source distribution includes the plugin entries and UI build sources.

## Voice configuration

Talk reads the selected host/profile's existing settings. For GPT-Live, configure
that host with `TALK_VOICE_MODE=live`. `TALK_LIVE_AUTH=subscription` is the default;
`TALK_LIVE_AUTH=api` must be selected explicitly. There is no automatic paid fallback.
Model and voice settings, account identity and credential handling use the existing
[GPT-Live configuration](GPT-LIVE.md#choose-billing-and-voice).

This Desktop entry supports GPT-Live and OpenAI Realtime (`native` voice mode).
Cascade streaming is not carried by this host's JSON plugin bridge; use the
dashboard for cascade. Unsupported modes produce an explanation before session
creation. Opening Talk does not alter your provider or billing settings.

The host keeps long-lived provider credentials. If `TALK_DASHBOARD_TOKEN` is
configured, the Talk window's existing token field supplies that additional gate;
it never replaces host authentication. Do not paste provider API keys there.

## Ownership and recovery

The host grants one microphone owner across built-in voice and Talk. Talk waits
for wake-listener suspension before opening audio. Closing, switching the
composer conversation, changing connection/profile or losing its lease stops the
transport and releases the microphone.

Requests capture the original connection and profile. An already admitted
request, including a late session receipt and its cleanup request, remains tied
to that owner. It cannot migrate to a newly focused conversation. The Talk task
picker still chooses the authorized task within that scope; the composer does
not grant authority over arbitrary Codex or Claude Code conversations.

If audio disconnects, inspect the task's current state before retrying an action.
Reopening Talk never automatically repeats an uncertain worker launch or send.

## Verification

Run `python scripts/build_ui.py --check`, then the focused Desktop, dashboard and
lease lifecycle regressions. The companion host must pass its composer ownership
and pinned REST tests and produce a working Desktop build.

On the installed candidate, verify the Talk action appears, start and stop audio,
switch conversations during pending work, and confirm results remain in their
original task. Test subscription and explicit API as separate operator-started
sessions. Automated transport tests do not establish microphone playback or
provider acceptance on a user's machine.

For rollback, close Desktop, restore the preserved host build and plugin directory,
then reopen it. Keep profiles, credential files and conversation data intact.

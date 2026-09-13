import * as HermesSDK from '@hermes/plugin-sdk'
import * as React from 'react'

const TALK_CSS = __HERMES_TALK_CSS__;
const DESKTOP_API = '/api/plugins/hermes-talk';
const h = React.createElement;
let desktopContext = null;

function ownerKey(owner) {
  return JSON.stringify([owner?.connectionId, owner?.profile,
    owner?.sessionId ?? null, owner?.storedSessionId ?? null]);
}

export function desktopAvailability(controller) {
  if (!controller || controller.capabilities?.microphoneLease !== 1 ||
      controller.capabilities?.pinnedRest !== 1) {
    return 'This Desktop build needs the microphone ownership and pinned plugin routing update. ' +
      'Use the matching Hermes host build described in the Talk Desktop guide.';
  }
  if (typeof controller.acquire !== 'function' ||
      typeof controller.owner?.connectionId !== 'string' || !controller.owner.connectionId ||
      typeof controller.owner?.profile !== 'string' || !controller.owner.profile) {
    return 'Open a connected Hermes conversation before starting Talk.';
  }
  return '';
}

export function createDesktopTalkSDK(context, controller) {
  const currentController = typeof controller === 'function' ? controller : () => controller;
  const unavailable = desktopAvailability(currentController());
  if (unavailable) throw new Error(unavailable);
  const owner = Object.freeze({ ...currentController().owner });
  const scope = Object.freeze({ connectionId: owner.connectionId, profile: owner.profile });
  return {
    React,
    hooks: React,
    components: { Button: HermesSDK.Button, Input: HermesSDK.Input },
    validateVoiceMode(status) {
      if (!status || !['live', 'native'].includes(status.voiceMode)) {
        throw new Error('Desktop Talk supports GPT-Live and OpenAI Realtime. ' +
          'Use the dashboard for cascade audio. Your voice settings have not been changed.');
      }
    },
    async acquireMicrophone({ signal } = {}) {
      const current = currentController();
      if (desktopAvailability(current) || ownerKey(current.owner) !== ownerKey(owner)) {
        throw new Error('The Hermes conversation changed. Reopen Talk in the selected conversation.');
      }
      const lease = await current.acquire({ signal });
      if (!lease || typeof lease.release !== 'function' || !lease.signal) {
        lease?.release?.();
        throw new Error('Hermes could not grant microphone ownership. Stop its other voice session first.');
      }
      return lease;
    },
    async fetchJSON(path, options = {}, timeoutMs) {
      if (!path.startsWith(DESKTOP_API + '/')) {
        throw new Error('Talk attempted to access a different plugin route.');
      }
      if (options.signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
      const suffix = path.slice(DESKTOP_API.length);
      const headers = new Headers(options.headers || {});
      // Keep in-flight receipts on their captured owner, including a late /close.
      // The shared UI's generation checks retire results after navigation.
      return context.rest(suffix, {
        method: options.method,
        body: typeof options.body === 'string' ? JSON.parse(options.body) : options.body,
        timeoutMs: timeoutMs || 30000,
        scope,
        pluginToken: headers.get('x-talk-token') || undefined,
      });
    },
  };
}

function DesktopTalkPanel({ context, controller }) {
  const controllerRef = React.useRef(controller);
  controllerRef.current = controller;
  const currentOwner = ownerKey(controller.owner);
  const surface = React.useMemo(() => createTalkSurface(
    createDesktopTalkSDK(context, () => controllerRef.current)), [context, currentOwner]);
  return h(React.Fragment, null,
    h('style', null, TALK_CSS),
    h(surface.TalkPage));
}

function DesktopTalkAction() {
  const useController = HermesSDK.useComposerVoiceController || (() => null);
  const controller = useController();
  const [openedOwner, setOpenedOwner] = React.useState(null);
  const currentOwner = ownerKey(controller?.owner);
  const unavailable = desktopAvailability(controller);
  const open = openedOwner !== null && openedOwner === currentOwner;
  React.useEffect(() => { setOpenedOwner(null); }, [currentOwner]);

  return h(React.Fragment, null,
    h(HermesSDK.Button, {
      type: 'button', variant: 'ghost', size: 'sm', title: 'Open Hermes Talk',
      'aria-label': 'Open Hermes Talk',
      onClick: () => setOpenedOwner(currentOwner),
    }, 'Talk'),
    h(HermesSDK.Dialog, {
      open,
      onOpenChange: value => setOpenedOwner(value ? currentOwner : null),
    }, h(HermesSDK.DialogContent, {
      className: 'w-[min(1100px,95vw)] max-w-[95vw]',
      bodyClassName: 'max-h-[85vh] overflow-y-auto',
      onSubmit: event => event.stopPropagation(),
    },
    h(HermesSDK.DialogHeader, null,
      h(HermesSDK.DialogTitle, null, 'Hermes Talk'),
      h(HermesSDK.DialogDescription, null,
        'Choose a task and start voice. Closing this window ends audio; accepted work continues.')),
    open && (unavailable || !desktopContext
      ? h('p', { role: 'status' }, unavailable || 'The Talk plugin is not ready.')
      : h(DesktopTalkPanel, { key: currentOwner, context: desktopContext, controller })))));
}

export default {
  id: 'hermes-talk',
  name: 'Hermes Talk',
  description: 'GPT-Live subscription or explicit API voice, with Hermes task delegation.',
  register(context) {
    desktopContext = context;
    context.register({
      id: 'talk', area: 'composer.actions', order: 45,
      render: () => h(DesktopTalkAction),
    });
    context.onDispose(() => {
      if (desktopContext === context) desktopContext = null;
    });
  },
};

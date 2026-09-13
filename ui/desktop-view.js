const DESKTOP_TALK_VIEW_CSS = `
.ht-desktop-view { display:grid; gap:1rem; min-width:0; color:inherit; font:inherit; }
.ht-desktop-view p, .ht-desktop-view h2, .ht-desktop-view h3 { margin:0; }
.ht-desktop-view h2 { font-size:1rem; font-weight:600; overflow-wrap:anywhere; }
.ht-desktop-view h3 { font-size:.875rem; font-weight:600; }
.ht-desktop-view .htd-row { display:flex; align-items:center; gap:.75rem; flex-wrap:wrap; }
.ht-desktop-view .htd-header { justify-content:space-between; }
.ht-desktop-view .htd-stack { display:grid; gap:.5rem; min-width:0; }
.ht-desktop-view .htd-muted { opacity:.7; font-size:.8125rem; }
.ht-desktop-view .htd-text { white-space:pre-wrap; overflow-wrap:anywhere; font-size:.875rem; }
.ht-desktop-view .htd-notice, .ht-desktop-view .htd-job { padding:.75rem; border:1px solid color-mix(in srgb,currentColor 18%,transparent); border-radius:.5rem; }
.ht-desktop-view .htd-captions { display:grid; gap:.75rem; max-height:15rem; overflow-y:auto; }
.ht-desktop-view .htd-compose { display:flex; align-items:center; gap:.5rem; }
.ht-desktop-view .htd-compose input { flex:1; min-width:0; }
.ht-desktop-view details { border-top:1px solid color-mix(in srgb,currentColor 18%,transparent); padding-top:.75rem; }
.ht-desktop-view summary { cursor:pointer; font-size:.8125rem; }
.ht-desktop-view details > .htd-stack { margin-top:.75rem; }
.ht-desktop-view label { display:grid; gap:.375rem; font-size:.8125rem; }
.ht-desktop-view select { width:100%; min-width:0; padding:.5rem; color:inherit; background:inherit; border:1px solid color-mix(in srgb,currentColor 25%,transparent); border-radius:.375rem; font:inherit; }
.ht-desktop-view select:disabled { opacity:.5; }
`;

function desktopTalkNotice(value, needsToken) {
  if (!value && !needsToken) return null;
  const message = String(value?.message || value || '');
  if (needsToken || /\b(?:401|403)\b/.test(message)) {
    return { text: 'Reconnect to this Hermes connection and try again.' };
  }
  if (/NotAllowedError|PermissionDenied|permission denied|microphone.*(?:denied|blocked)|(?:denied|blocked).*microphone/i.test(message)) {
    return { text: 'Allow microphone access for Hermes in your system settings, then try again.', retry: true };
  }
  if (/NotFoundError|DevicesNotFound|no microphone|microphone.*not found/i.test(message)) {
    return { text: 'Connect a microphone, then try again.', retry: true };
  }
  if (/NotReadableError|TrackStartError|microphone.*(?:in use|busy)|other voice session/i.test(message)) {
    return { text: 'Stop the other voice session using your microphone, then try again.', retry: true };
  }
  if (/\b503\b|temporarily unavailable|service_unavailable/i.test(message)) {
    return { text: 'Talk is temporarily unavailable. Try again.', retry: true };
  }
  return { text: 'Reconnect to this Hermes connection and try again.' };
}

function desktopTalkSource(source) {
  if (['codex-oauth', 'subscription'].includes(source)) return 'ChatGPT subscription';
  if (['configured', 'env', 'api'].includes(source)) return 'OpenAI API';
  return 'Hermes voice';
}

function desktopTalkLiveLabel(live) {
  if (/^Thinking|^Processing/i.test(live)) return 'Thinking…';
  if (/^Using /i.test(live)) return 'Hermes is working on your request.';
  if (/checking the request/i.test(live)) return 'Hermes is checking your request.';
  if (/returned a task decision/i.test(live)) return 'Hermes returned an update.';
  return 'Listening…';
}

function desktopTalkJobLabel(status) {
  return ({ queued: 'Queued', pending: 'Waiting', accepted: 'Accepted', running: 'In progress',
    completed: 'Complete', succeeded: 'Complete', failed: 'Failed', cancelled: 'Cancelled',
    waiting_approval: 'Needs approval', approval_required: 'Needs approval', paused: 'Paused' })[status]
    || 'Waiting for an update';
}

export function DesktopTalkView(props) {
  const h = React.createElement;
  const { status, loading, ready, active, starting, live, error, catalogError, needsToken,
    selectedTask, taskState, typed = '', sending, switching, returnDepth, voice = '',
    startTalk, stopTalk, refresh, refreshCatalog, setTyped, sendTyped, switchTarget,
    showResult, saveUpdatePreference, setVoice } = props;
  const tasks = (props.tasks || []).filter(task => typeof task?.target_id === 'string' && task.target_id);
  const selected = tasks.find(task => task.target_id === selectedTask);
  const conversation = selected?.label || taskState?.task?.label || 'This conversation';
  const jobs = taskState?.jobs || [];
  const results = Object.entries(props.results || {});
  const captions = (props.transcript || []).filter(row => typeof row?.text === 'string' && row.text.length);
  const voices = (status?.voices || []).filter(name => typeof name === 'string');
  const notice = desktopTalkNotice(error, needsToken);
  const catalogNotice = desktopTalkNotice(catalogError, false);
  const button = (label, onClick, options = {}) => h(HermesSDK.Button,
    { ...options, type: 'button', onClick }, label);
  const busy = starting || switching;

  return h('section', { className: 'ht-desktop-view', 'aria-label': 'Talk in this conversation' },
    h('style', null, DESKTOP_TALK_VIEW_CSS),
    h('div', { className: 'htd-row htd-header' },
      h('div', { className: 'htd-stack' },
        h('h2', null, conversation),
        h('p', { className: 'htd-muted', role: 'status' },
          loading ? 'Checking connection…' : starting ? 'Connecting…'
            : active ? desktopTalkSource(status?.source) + ' · ' + desktopTalkLiveLabel(live)
            : ready ? desktopTalkSource(status?.source) : 'Talk is not ready on this connection.')),
      active || starting
        ? button(starting ? 'Cancel connection' : 'Stop talking', stopTalk)
        : button('Start talking', () => void startTalk(),
          { disabled: !ready || loading || switching || needsToken })),

    !active && !starting && ready && !notice && h('p', { className: 'htd-muted' },
      'Talk to Hermes in this conversation. You can interrupt at any time.'),
    notice && h('div', { className: 'htd-stack htd-notice', role: 'alert' },
      h('p', { className: 'htd-text' }, notice.text),
      h('div', null, button(notice.retry ? 'Try again' : 'Check connection',
        () => void (notice.retry && ready && !active ? startTalk() : refresh()),
        { variant: 'outline', size: 'sm', disabled: loading || busy }))),
    !ready && !loading && !notice && h('div', { className: 'htd-stack' },
      h('p', { className: 'htd-muted' }, 'Reconnect to this Hermes connection and try again.'),
      h('div', null, button('Check connection', () => void refresh(),
        { variant: 'outline', size: 'sm', disabled: busy }))),

    captions.length > 0 && h('section', { className: 'htd-stack', 'aria-label': 'Live captions' },
      h('h3', null, 'Live captions'),
      h('div', { className: 'htd-captions', role: 'log', 'aria-live': 'polite' },
        captions.map((row, index) => h('div', { className: 'htd-stack', key: row.id ?? index },
          h('span', { className: 'htd-muted' }, row.role === 'user' ? 'You' : 'Hermes'),
          h('p', { className: 'htd-text' }, row.text))))),

    active && h('form', { className: 'htd-compose', onSubmit: event => {
      event.preventDefault();
      event.stopPropagation();
      if (!sending && !switching && typed.trim()) void sendTyped();
    } },
    h(HermesSDK.Input, { value: typed, disabled: sending || switching,
      placeholder: 'Or type a message…', 'aria-label': 'Message Hermes',
      onChange: event => setTyped(event.target.value) }),
    h(HermesSDK.Button, { type: 'submit', disabled: sending || switching || !typed.trim() },
      sending ? 'Sending…' : 'Send')),

    jobs.length > 0 && h('section', { className: 'htd-stack', 'aria-label': 'Background work' },
      h('h3', null, 'Background work'),
      h('p', { className: 'htd-muted' }, 'Accepted work keeps running after you stop talking.'),
      jobs.map(job => h('article', { className: 'htd-stack htd-job', key: job.run_id },
        h('p', { className: 'htd-text' }, job.goal || 'Background task'),
        h('p', { className: 'htd-muted' }, desktopTalkJobLabel(job.status)),
        job.result_available && !props.results?.[job.run_id] && h('div', null,
          button('Show result', () => void showResult(job.run_id),
            { variant: 'outline', size: 'sm', disabled: !active || switching }))))),

    results.length > 0 && h('section', { className: 'htd-stack', 'aria-label': 'Task results' },
      h('h3', null, 'Results'),
      results.map(([runId, result]) => h('article', { className: 'htd-stack htd-job', key: runId },
        h('p', { className: 'htd-muted' },
          jobs.find(job => job.run_id === runId)?.goal || 'Task result'),
        h('p', { className: 'htd-text' }, typeof result?.output === 'string' && result.output
          ? result.output : result?.error ? 'This task could not return a result.' : 'No result text was supplied.'),
        result?.truncated && h('p', { className: 'htd-muted' },
          'Only part of this result is available here.')))),

    h('details', null, h('summary', null, 'Advanced'),
      h('div', { className: 'htd-stack' },
        voices.length > 0 && h('label', null, 'Voice',
          h('select', { value: voice, disabled: !ready || active || busy,
            onChange: event => setVoice(event.target.value) },
          h('option', { value: '' }, 'Use configured voice'),
          voices.map(name => h('option', { value: name, key: name }, name)))),
        tasks.length > 0 && h('label', null, 'Switch conversation',
          h('select', { value: selectedTask || '', disabled: !active || busy,
            onChange: event => {
              if (event.target.value && event.target.value !== selectedTask)
                void switchTarget({ target_id: event.target.value });
            } },
          h('option', { value: '', disabled: true }, 'Choose a conversation'),
          tasks.map((task, index) => h('option', { value: task.target_id, key: task.target_id },
            (task.label || 'Conversation ' + (index + 1)) +
            (tasks.filter(other => other.label === task.label).length > 1
              ? ' · ' + String(task.session_id || task.target_id).slice(-6) : ''))))),
        returnDepth > 0 && h('div', null,
          button('Return to previous conversation', () => void switchTarget({ back: true }),
            { variant: 'outline', size: 'sm', disabled: !active || busy })),
        catalogNotice && h('p', { className: 'htd-muted', role: 'status' },
          'The conversation list is unavailable. ' + catalogNotice.text),
        h('div', null, button('Refresh conversations', () => void refreshCatalog(),
          { variant: 'outline', size: 'sm', disabled: loading || busy })),
        taskState && h('label', null, 'Spoken updates',
          h('select', { value: taskState.preferences?.update_mode || 'important',
            disabled: !active || busy,
            onChange: event => void saveUpdatePreference(event.target.value) },
          h('option', { value: 'important' }, 'Completion and important updates'),
          h('option', { value: 'completion' }, 'Completion only'),
          h('option', { value: 'frequent' }, 'Include meaningful milestones'))))));
}

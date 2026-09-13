"""Exercise the bundled Desktop entry against the host SDK boundary."""

from pathlib import Path
from subprocess import run

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const calls = [], registered = [], disposers = [];
const React = {createElement(type, props, ...children) {return {type, props, children};}};
const HermesSDK = {Button:'button',Input:'input'};
const context = vm.createContext({React,HermesSDK,Headers,DOMException,AbortController,
  console, window:{setTimeout,clearTimeout,sessionStorage:{getItem(){return '';}}}});
const source = fs.readFileSync(process.argv[1],'utf8')
  .replace(/^import .* from .*\r?\n/gm,'')
  .replace(/export function /g,'function ')
  .replace('export default {','globalThis.plugin = {');
vm.runInContext(source,context);
const owner = {connectionId:'connection-a',profile:'profile-a',sessionId:'pane-a',
  storedSessionId:'stored-a'};
const leaseController = new AbortController();
let acquires = 0, releases = 0;
const controller = {capabilities:{microphoneLease:1,pinnedRest:1},owner,
  async acquire() {acquires++;return {signal:leaseController.signal,release(){releases++;}};}};
const host = {rest(path,options) {calls.push({path,options});return Promise.resolve({ok:true});},
  register(entry) {registered.push(entry);},onDispose(fn){disposers.push(fn);}};
const createSDK=()=>context.createDesktopTalkSDK(host,controller);
"""


def run_node(script):
    result = run(
        ["node", "-e", HARNESS + script, str(ROOT / "desktop/plugin.js")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_desktop_pins_route_token_and_late_receipt():
    run_node(r"""
(async()=>{
const sdk=createSDK(); let finish;
host.rest=(path,options)=>{calls.push({path,options});return new Promise(r=>{finish=r;});};
const abort = new AbortController();
const pending=sdk.fetchJSON('/api/plugins/hermes-talk/session',{
  method:'POST',body:JSON.stringify({taskId:'task-a'}),signal:abort.signal,
  headers:{'x-talk-token':'talk-gate','Authorization':'Bearer unrelated-host-token'}},17500);
controller.owner={...owner,connectionId:'connection-b',profile:'profile-b'};
abort.abort(); finish({receipt:'session-a'});
assert.equal((await pending).receipt,'session-a');
assert.equal(calls[0].options.scope.connectionId,'connection-a');
assert.equal(calls[0].options.scope.profile,'profile-a');
assert.equal(calls[0].options.pluginToken,'talk-gate');
assert.equal(calls[0].options.timeoutMs,17500);
assert.equal(calls[0].options.body.taskId,'task-a');
assert.equal(calls[0].options.headers,undefined);
host.rest=async(path,options)=>{calls.push({path,options});return {};};
await sdk.fetchJSON('/api/plugins/hermes-talk/close',{
  method:'POST',body:JSON.stringify({sessionId:'session-a'})});
assert.equal(calls[1].options.scope.connectionId,'connection-a');
assert.equal(calls[1].options.scope.profile,'profile-a');
await assert.rejects(sdk.acquireMicrophone(),/conversation changed/);
assert.equal(acquires,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


@pytest.mark.parametrize("scenario", [
    r"""
controller.capabilities={microphoneLease:1};
assert.throws(createSDK,/Desktop build needs/);
""",
    r"""
controller.owner=null; assert.throws(createSDK,/connected Hermes conversation/);
""",
    r"""
const sdk=createSDK(); const abort=new AbortController();abort.abort();
await assert.rejects(sdk.fetchJSON('/api/plugins/hermes-talk/status',{
  signal:abort.signal}),{name:'AbortError'});assert.equal(calls.length,0);
""",
    r"""
const sdk=createSDK();
await assert.rejects(sdk.fetchJSON('/api/plugins/another-plugin/status'),/different plugin/);
await assert.rejects(sdk.fetchJSON('/api/plugins/hermes-talk/session',{body:'{broken'}));
assert.equal(calls.length,0);
""",
    r"""
const sdk=createSDK();
sdk.validateVoiceMode({voiceMode:'live'});sdk.validateVoiceMode({voiceMode:'native'});
assert.throws(()=>sdk.validateVoiceMode({voiceMode:'cascade'}),/supports GPT-Live/);
assert.throws(()=>sdk.validateVoiceMode({voiceMode:'unrecognized'}));
assert.equal(acquires,0);assert.equal(calls.length,0);
""",
    r"""
controller.acquire=async()=>null;
await assert.rejects(createSDK().acquireMicrophone(),/microphone ownership/);
""",
    r"""
controller.acquire=async()=>({release(){releases++;}});
await assert.rejects(createSDK().acquireMicrophone(),/microphone ownership/);
assert.equal(releases,1);
""",
    r"""
const lease=await createSDK().acquireMicrophone();assert.equal(acquires,1);
assert.equal(lease.signal,leaseController.signal);lease.release();assert.equal(releases,1);
""",
])
def test_desktop_rejects_unavailable_or_invalid_operations(scenario):
    run_node("\n(async()=>{\n" + scenario + r"""
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_desktop_registers_inside_the_composer_without_starting_audio():
    run_node(r"""
context.plugin.register(host);
assert.equal(context.plugin.id,'hermes-talk');assert.equal(registered.length,1);
assert.equal(registered[0].area,'composer.actions');
assert.equal(typeof registered[0].render().type,'function');
assert.equal(acquires,0);assert.equal(calls.length,0);
assert.equal(disposers.length,1);disposers[0]();
""")


def test_same_owner_controller_refresh_preserves_page_and_updates_acquisition():
    run_node(r"""
(async()=>{
let ref, memo, deps;
React.useRef=value=>ref||(ref={current:value});
React.useMemo=(factory,next)=>{
  if(!deps || deps.some((value,index)=>value!==next[index])){memo=factory();deps=next;}
  return memo;
};
const first=context.DesktopTalkPanel({context:host,controller});
let latest=controller, refreshedAcquires=0;
const sdk=context.createDesktopTalkSDK(host,()=>latest);
const refreshed={...controller,owner:{...owner},async acquire(){
  refreshedAcquires++;return {signal:leaseController.signal,release(){}};}};
latest=refreshed;
const second=context.DesktopTalkPanel({context:host,controller:refreshed});
assert.equal(first.children[1].type,second.children[1].type,
  'same-owner controller refresh must retain the mounted TalkPage');
await sdk.acquireMicrophone();assert.equal(refreshedAcquires,1);assert.equal(acquires,0);
const third=context.DesktopTalkPanel({context:host,
  controller:{...refreshed,owner:{...owner,storedSessionId:'different-task'}}});
assert.notEqual(second.children[1].type,third.children[1].type);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_opening_talk_cannot_submit_the_composer_draft():
    run_node(r"""
React.useState=()=>[null,()=>{}];React.useEffect=()=>{};
HermesSDK.useComposerVoiceController=()=>controller;
const tree=context.DesktopTalkAction();
assert.equal(tree.children[0].props.type,'button');
tree.children[0].props.onClick();assert.equal(calls.length,0);assert.equal(acquires,0);
let stopped=0;
tree.children[1].children[0].props.onSubmit({stopPropagation(){stopped++;}});
assert.equal(stopped,1,'Talk form submits must stop before the host composer');
""")

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../preparation-pages.js', import.meta.url), 'utf8');
const { startPreparationPages } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const token = 'b'.repeat(64);
const marked = `https://labs.google/fx/tools/flow#flow-console-page=8001.${token}`;
let passed = 0;
async function check(name, options, expected) {
  const tabs = [{ id: 1, url: 'https://labs.google/fx/tools/flow/project/user' },
                { id: 2, url: options.url ?? marked }, ...(options.extraTabs || [])];
  const removed = [];
  const sockets = [];
  let saved = options.saved ?? {};
  let receiveLaunch;
  let probes = 0;
  const event = () => ({ addListener() {} });
  globalThis.chrome = {
    runtime: { onMessage: { addListener: fn => { receiveLaunch = fn; } } },
    storage: { local: { set: async () => {} }, session: { get: async () => saved, set: async value => { saved = value; } } },
    tabs: {
      query: async () => tabs,
      get: async id => { const tab = tabs.find(t => t.id === id); if (!tab) throw Error('gone'); return tab; },
      remove: async id => {
        if (options.removeFails) throw Error('denied');
        if (options.noRemovalEffect) return;
        removed.push(id); const index=tabs.findIndex(t=>t.id===id); if(index>=0) tabs.splice(index,1);
      },
      onCreated: event(), onUpdated: event(), onRemoved: event(),
    },
    windows: { getAll: async () => [{id: 10}] },
    scripting: { executeScript: async ({target}) => {
      probes++;
      if (options.probeFails) throw Error('no access');
      if (options.navigateDuringProbe && probes > 1) tabs[1].url = 'https://example.com';
      if (options.navigateAuxiliaryDuringPrimaryProbe && probes > 3 && target.tabId === 2) tabs.find(t => t.id === 3).url = 'https://example.com';
      return [{ result: (target.tabId === options.challengeTab) || (options.challenge ?? false) }];
    } },
    alarms: { create() {}, onAlarm: event() },
  };
  globalThis.WebSocket = class {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 1; this.messages = []; sockets.push(this); }
    send(message) { this.messages.push(message); }
    close() { this.readyState = 3; }
  };
  const preparation = startPreparationPages(() => !options.busy);
  await preparation;
  if (options.earlyLaunch) {
    receiveLaunch({ flowConsolePreparation: marked }, { tab: { id: 2 }, url: tabs[1].url });
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  if (!options.noOwner) {
    assert.deepEqual(await preparation.scopedTabIds(), [], 'restored markers are not authenticated leases');
    await sockets[0].onmessage({data: JSON.stringify({action:'wait'})});
  }
  for (const id of options.createdIds || []) await preparation.trackCreatedTab(tabs.find(t => t.id === id));
  if (options.noOwner) { assert.equal(sockets.length, 0); }
  else {
    const scoped = await preparation.scopedTabIds();
    assert.ok(scoped.includes(2), 'the current launch tab is in browser operation scope');
    assert.ok(!scoped.includes(1), 'pre-existing user tab is outside browser operation scope');
    for (const id of options.createdIds || []) assert.ok(scoped.includes(id));
    assert.equal(sockets.length, options.stalePage ? 2 : 1);
    if (options.stalePage) assert.ok(!scoped.includes(3), 'a restored, rejected nonce must not hijack scope');
    assert.ok(sockets[0].url.startsWith('ws://127.0.0.1:8001/console/api/accounts/preparation/page/'));
    if (options.reconnect) {
      sockets[0].onclose();
      await new Promise(resolve => setTimeout(resolve, 1100));
      assert.equal(sockets.length, 2, 'reload contention reconnects only the local page channel');
      assert.equal(sockets[1].url, sockets[0].url);
    }
    const activeSocket = options.stalePage ? sockets[0] : sockets.at(-1);
    if (options.afterNavigate) tabs[1].url = options.afterNavigate;
    await activeSocket.onmessage({ data: JSON.stringify({ action: options.action ?? 'close' }) });
    if (expected === 'closed' || options.removeFails || options.noRemovalEffect || options.commitRace) {
      assert.equal(activeSocket.messages.at(-1), 'ready');
      assert.deepEqual(removed, [], 'ready handshake alone cannot remove a tab');
      await activeSocket.onmessage({ data: JSON.stringify({ action: 'commit_close' }) });
    }
    if (expected === 'closed') {
      assert.deepEqual(JSON.parse(activeSocket.messages.at(-1)), { state: 'closed', remaining_windows: 1, remaining_tabs: tabs.length });
    } else if (expected) assert.equal(activeSocket.messages.at(-1), expected);
    if (options.noReconnectAfterRetain) {
      activeSocket.onclose();
      await new Promise(resolve => setTimeout(resolve, 1100));
      assert.equal(sockets.length, 1, 'retaining a page revokes reconnect and cleanup ownership');
    }
  }
  assert.deepEqual(removed, expected === 'closed' ? [...(options.createdIds || []), 2] : []);
  assert.ok(!removed.includes(1), 'user-created tab must never close');
  passed++;
  console.log(`✓ ${name}`);
}
await check('closes the owned preparation tab and keeps existing user tab', {}, 'closed');
await check('no launch marker means no close ownership', { url: 'https://labs.google/fx/tools/flow', noOwner: true });
await check('failed account check retains page', { action: 'retain' });
await check('reload socket contention reconnects and still closes only its owned page', { reconnect: true }, 'closed');
await check('retaining a page cancels its reconnect permission', { action: 'retain', noReconnectAfterRetain: true });
await check('check in progress cannot close', { action: 'wait' }, 'waiting');
await check('challenge wait reports attention', {action: 'wait', challenge: true}, 'attention');
await check('login wait reports attention', {action: 'wait', afterNavigate:'https://accounts.google.com/signin'}, 'attention');
await check('in-flight generation operation defers closing', { busy: true }, 'waiting');
await check('visible challenge is retained', { challenge: true }, 'retained');
await check('login redirect is retained', { afterNavigate: 'https://accounts.google.com/signin' }, 'retained');
await check('user navigation outside Flow is retained', { afterNavigate: 'https://example.com' }, 'retained');
await check('probe failure cannot authorize closure', { probeFails: true }, 'retained');
await check('navigation during async probe never closes new destination', { navigateDuringProbe: true }, 'waiting');
await check('Chrome removal failure is not reported closed', { removeFails: true }, 'retained');
await check('a resolved removal promise cannot report closed while the tab still exists', { noRemovalEffect: true }, 'retained');
await check('commit without ownership handshake cannot close', { action: 'commit_close' }, 'retained');
await check('document-start capture survives Flow router replacing launch URL', {
  url: 'https://labs.google/fx/tools/flow/project/routed', earlyLaunch: true,
}, 'closed');
await check('worker restart recovers concrete tab ID from session storage', {
  url: 'https://labs.google/fx/tools/flow/project/changed',
  saved: { consolePreparationPages: [{ tabId: 2, port: 8001, token, expires: Date.now() + 100000 }] },
}, 'closed');
await check('closes all extension-created bootstrap pages before its launch tab', {
  extraTabs: [{id: 3, url: 'https://flow.google.com/project/bootstrap'}, {id: 4, url: 'https://labs.google/fx/tools/flow/project/bootstrap2'}],
  createdIds: [3, 4],
}, 'closed');
await check('a pre-existing Flow page in the same browser is never adopted by domain', {
  extraTabs: [{id: 3, url: 'https://flow.google.com/project/user'}],
}, 'closed');
await check('challenge in an auxiliary bootstrap page retains the entire preparation group', {
  extraTabs: [{id: 3, url: 'https://flow.google.com/project/bootstrap'}], createdIds: [3], challengeTab: 3,
}, 'retained');
await check('worker suspension preserves ownership of auxiliary tabs as well', {
  url: 'https://labs.google/fx/tools/flow/project/routed',
  extraTabs: [{id: 3, url: 'https://flow.google.com/project/bootstrap'}], createdIds: [3],
  saved: {consolePreparationPages: [{tabId: 2, port: 8001, token, createdTabIds: [3], expires: Date.now() + 100000}]},
}, 'closed');
await check('user navigation during another tab probe revokes group removal', {
  extraTabs: [{id: 3, url: 'https://flow.google.com/project/bootstrap'}], createdIds: [3],
  navigateAuxiliaryDuringPrimaryProbe: true, commitRace: true,
}, 'waiting');
await check('a restored stale launch never overrides the currently authenticated task', {
  stalePage: true,
  extraTabs: [{id:3,url:`https://flow.google.com/#flow-console-page=8001.${'c'.repeat(64)}`}],
}, 'closed');
console.log(`${passed}/${passed} passed`);

// Concrete tab IDs created with a per-launch console nonce, never all Flow tabs.
// Session storage survives worker suspension, but not a browser restart/update.
const KEY = 'consolePreparationPages';
const ALARM = 'consolePreparationPages';

export function startPreparationPages(canClose = () => true) {
  const diagnostic = stage => { void chrome.storage.local.set({ consolePreparationDiagnostic: { stage, at: Date.now() } }).catch(() => {}); };
  diagnostic('started');
  const pages = new Map();
  const sockets = new Map();
  const save = () => chrome.storage.session.set({ [KEY]: [...pages.values()] });
  const forget = async (tabId) => {
    pages.delete(tabId);
    await save();
    sockets.get(tabId)?.close();
    sockets.delete(tabId);
  };
  const flowUrl = (value) => {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && (url.hostname === 'labs.google' || url.hostname === 'flow.google.com');
    } catch { return false; }
  };
  function connect(page) {
    if (sockets.has(page.tabId)) return;
    diagnostic('connecting');
    const ws = new WebSocket(`ws://127.0.0.1:${page.port}/console/api/accounts/preparation/page/${page.token}`);
    sockets.set(page.tabId, ws);
    let handling = false;
    let readyToClose = false;
    ws.onopen = () => { page.localReconnects = 0; diagnostic('connected'); };
    ws.onmessage = async ({ data }) => {
      if (handling) return;
      handling = true;
      try {
        const { action } = JSON.parse(data);
        // A restored fragment is not a live console lease. Only the service
        // holding this exact nonce can acknowledge it. Never use a stale page
        // as a generation target just because it was listed last.
        if (['wait', 'close', 'commit_close', 'retain'].includes(action)) page.authorized = true;
        if (action === 'retain') { await forget(page.tabId); return; }
        if (action === 'wait') {
          // Inspect only this nonce-owned tab. Report a need for human input;
          // never interact with credentials or solve verification controls.
          let attention = false;
          let inspected = false;
          try {
            const tab = await chrome.tabs.get(page.tabId);
            attention = /^https:\/\/accounts\.google\.com\//.test(tab?.url || '');
            if (!attention && flowUrl(tab?.url)) {
              const checks = await chrome.scripting.executeScript({ target: { tabId: page.tabId },
                func: () => [...document.querySelectorAll('input[type="password"],input[type="email"],#captchaimg,iframe[src*="recaptcha"][src*="bframe"]')]
                  .some(node => { const box = node.getBoundingClientRect(); const style = getComputedStyle(node);
                    return box.width > 10 && box.height > 10 && style.display !== 'none' && style.visibility !== 'hidden'; }),
              });
              inspected = checks.length > 0 && checks.every(c => typeof c.result === 'boolean');
              attention = checks.some(c => c.result === true);
            }
          } catch { /* An unreadable page is not authority to reveal unrelated windows. */ }
          ws.send(attention ? 'attention' : inspected ? 'waiting' : 'unknown'); return;
        }

        if (!['close', 'commit_close'].includes(action) || !canClose()) { ws.send('waiting'); return; }
        if (action === 'commit_close' && !readyToClose) { ws.send('retained'); await forget(page.tabId); return; }
        const tabIds = [...(page.createdTabIds || []), page.tabId];
        const targets = [];
        for (const id of tabIds) {
          const tab = await chrome.tabs.get(id).catch(() => null);
          if (!tab && id !== page.tabId) continue; // An auxiliary tab already closed.
          if (!tab || !flowUrl(tab.url)) {
            ws.send('retained'); await forget(page.tabId); return;
          }
          // Detect visible login/verification controls. Never solve a challenge.
          const checks = await chrome.scripting.executeScript({
            target: { tabId: id },
            func: () => [...document.querySelectorAll('input[type="password"],input[type="email"],#captchaimg,iframe[src*="/recaptcha/"][src*="bframe"]')]
              .some(node => {
                const box = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return box.width > 10 && box.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
              }),
          });
          if (!checks.length || checks.some(c => c.result !== false)) {
            ws.send('retained'); await forget(page.tabId); return;
          }
          // Recheck after the asynchronous DOM probe, before removing anything.
          const current = await chrome.tabs.get(id);
          if (!flowUrl(current.url) || current.url !== tab.url || !canClose()) { ws.send('waiting'); return; }
          targets.push({ id, url: tab.url });
        }
        if (action === 'close') { readyToClose = true; ws.send('ready'); return; }
        // Remove extension-created auxiliary tabs first. The marked launch tab
        // is last so its window cannot survive just because bootstrap opened
        // another Flow page. Never include a tab merely found by tabs.query.
        for (const target of targets) {
          // Another tab's asynchronous probe may have allowed this one to
          // navigate. Recheck each concrete target immediately before removal.
          const current = await chrome.tabs.get(target.id);
          if (!canClose() || current.url !== target.url || !flowUrl(current.url)) { ws.send('waiting'); return; }
          await chrome.tabs.remove(target.id);
          if (await chrome.tabs.get(target.id).catch(() => null)) {
            ws.send('retained'); await forget(page.tabId); return;
          }
        }
        // Report counts only, never titles/URLs of the user's other pages.
        const remainingWindows = await chrome.windows.getAll({ populate: false });
        const remainingTabs = await chrome.tabs.query({});
        ws.send(JSON.stringify({ state: 'closed', remaining_windows: remainingWindows.length,
          remaining_tabs: remainingTabs.length }));
        await forget(page.tabId);
      } catch {
        if (ws.readyState === WebSocket.OPEN) ws.send('retained');
        await forget(page.tabId);
      } finally { handling = false; }
    };
    ws.onclose = () => {
      if (sockets.get(page.tabId) !== ws) return;
      page.authorized = false;
      sockets.delete(page.tabId);
      // runtime.reload can reconnect before the old worker's socket has been
      // released by the console. Retry ONLY this local cleanup channel, never
      // a generation RPC. Forget/expiry revokes the retry as well as closing.
      page.localReconnects = (page.localReconnects || 0) + 1;
      if (pages.get(page.tabId) === page && page.expires > Date.now() && page.localReconnects <= 12) {
        setTimeout(() => {
          if (pages.get(page.tabId) === page && page.expires > Date.now()) connect(page);
        }, 1000);
      }
    };
    ws.onerror = () => diagnostic('channel_error'); // Missing console cannot authorize closure.
  }
  async function adopt(tab) {
    if (!flowUrl(tab?.pendingUrl || tab?.url)) return;
    const url = new URL(tab.pendingUrl || tab.url);
    const match = /^#flow-console-page=(\d{1,5})\.([a-f0-9]{64})$/.exec(url.hash);
    if (!match || +match[1] < 1 || +match[1] > 65535 || pages.has(tab.id)) return;
    if ([...pages.values()].some(p => p.token === match[2])) return;
    const page = { tabId: tab.id, port: +match[1], token: match[2], expires: Date.now() + 1800000 };
    pages.set(tab.id, page);
    diagnostic('owned_tab_found');
    await save();
    connect(page);
  }
  const ready = (async () => {
    const saved = await chrome.storage.session.get(KEY);
    for (const page of saved[KEY] || []) {
      if (page.expires > Date.now() && await chrome.tabs.get(page.tabId).catch(() => null)) {
        page.authorized = false;
        pages.set(page.tabId, page); connect(page);
      }
    }
    for (const tab of await chrome.tabs.query({})) await adopt(tab);
  })().catch(() => diagnostic('initialization_error'));
  ready.trackCreatedTab = async tab => {
    await ready;
    // Called ONLY immediately after this extension's chrome.tabs.create.
    // Do not infer ownership from domain, window membership, or tab order.
    const active = [...pages.values()].filter(p => p.expires > Date.now() && p.authorized);
    if (active.length !== 1 || !flowUrl(tab?.pendingUrl || tab?.url)) return;
    const page = active[0];
    page.createdTabIds = [...new Set([...(page.createdTabIds || []), tab.id])].filter(id => id !== page.tabId);
    await save();
  };
  // During a console run, browser operations must use only that run's tabs.
  // Do not navigate a user's earlier Flow project to prepare verification.
  ready.scopedTabIds = async () => {
    await ready;
    const present = [...pages.values()].filter(p => p.expires > Date.now());
    if (!present.length) return null;
    const active = present.filter(p => p.authorized);
    if (!active.length) return [];
    const page = active[active.length - 1];
    return [page.tabId, ...(page.createdTabIds || [])];
  };
  chrome.tabs.onCreated.addListener(tab => { void ready.then(() => adopt(tab)).catch(() => {}); });
  chrome.runtime.onMessage.addListener((message, sender) => {
    if (!message?.flowConsolePreparation || !sender.tab?.id || !flowUrl(sender.url)) return false;
    // A content script may supply only the launch URL, never a target tab ID.
    void ready.then(() => adopt({ id: sender.tab.id, url: message.flowConsolePreparation })).catch(() => {});
    return false;
  });
  chrome.tabs.onUpdated.addListener((_id, _change, tab) => { void ready.then(() => adopt(tab)).catch(() => {}); });
  chrome.tabs.onRemoved.addListener(tabId => {
    // Keep the channel alive until tabs.remove's acknowledgement has been sent.
    if (pages.has(tabId) && !sockets.has(tabId)) void forget(tabId);
  });
  chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener(alarm => {
    if (alarm.name !== ALARM) return;
    void ready.then(async () => {
      for (const page of pages.values()) {
        if (page.expires <= Date.now()) await forget(page.tabId);
        else connect(page);
      }
    }).catch(() => {});
  });
  return ready;
}

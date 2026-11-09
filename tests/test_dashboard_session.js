const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    let dashboardSessionToken ='), html.indexOf('    // In-dashboard confirm/notice dialog.'));

function session(tokens) {
  const controls = new Map();
  const pending = [];
  const requests = [];
  let connections = 0;
  let closed = 0;
  const originalSocket = {onopen() {}, onmessage() {}, onclose() {}, onerror() {}, close() { closed++; }};
  const context = vm.createContext({
    ws: originalSocket, wsReconnectTimer: null,
    operationIssues: new Map([['old-auth', {message: 'Dashboard capability token required'}], ['profile', {message: 'A profile needs repair'}]]),
    location: {pathname: '/dashboard', search: '?view=settings', reload() { throw new Error('Must preserve open editors'); }},
    document: {getElementById(id) { if (!controls.has(id)) controls.set(id, {value: 'unsaved profile name'}); return controls.get(id); }},
    fetch: async (url, options) => { requests.push({url, options}); return {ok: true, text: async () => tokens.shift()}; },
    DOMParser: class { parseFromString(token) { return {querySelector() { return {getAttribute: () => token}; }}; } },
    connectWS() { connections++; }, renderNotificationBell() {}, clearTimeout() {},
    stopHeartbeat() {}, setCompanionConnected() {},
    setTimeout(callback, delay) { pending.push({callback, delay}); return pending.length; },
  });
  vm.runInContext(code, context);
  return {context, controls, pending, requests, originalSocket, connections: () => connections, closed: () => closed};
}

test('expired sessions fetch fresh same-origin credentials once without reloading or discarding drafts', async () => {
  const token = 'a'.repeat(43);
  const state = session([token]);
  state.context.document.getElementById('profileName');
  const first = state.context.refreshDashboardSession();
  assert.equal(state.context.refreshDashboardSession(), first);
  await first;
  assert.equal(state.requests.length, 1);
  assert.equal(state.requests[0].url, '/dashboard?view=settings');
  assert.equal(state.requests[0].options.cache, 'no-store');
  assert.equal(state.requests[0].options.credentials, 'same-origin');
  assert.equal(state.requests[0].options.redirect, 'error');
  assert.equal(state.connections(), 1);
  assert.equal(state.closed(), 1);
  assert.equal(state.originalSocket.onclose, null);
  assert.equal(state.controls.get('profileName').value, 'unsaved profile name');
  assert.equal(vm.runInContext('dashboardSessionToken', state.context), token);
  assert.equal(state.context.operationIssues.has('old-auth'), false);
  assert.equal(state.context.operationIssues.has('profile'), true);
});

test('a second Companion restart renews the session again', async () => {
  const state = session(['a'.repeat(43), 'b'.repeat(43)]);
  await state.context.refreshDashboardSession();
  await state.context.refreshDashboardSession();
  assert.equal(state.connections(), 2);
  assert.equal(vm.runInContext('dashboardSessionToken', state.context), 'b'.repeat(43));
});

test('missing or unchanged credentials retry without weakening authorization or looping page reloads', async () => {
  for (const tokens of [['not-a-session'], ['a'.repeat(43), 'a'.repeat(43)]]) {
    const state = session(tokens.slice());
    if (tokens.length === 2) await state.context.refreshDashboardSession();
    const previous = state.connections();
    await state.context.refreshDashboardSession();
    assert.equal(state.connections(), previous);
    assert.equal(state.pending.length, 1);
    assert.equal(state.pending[0].delay, 3000);
  }
});

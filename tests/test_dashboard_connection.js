const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const healthCode = html.slice(html.indexOf('    let wsHeartbeatTimer ='), html.indexOf('    let dashboardSessionToken ='));

function dashboard() {
  let now = 0;
  let tick;
  let cleared = 0;
  const nodes = new Map();
  const status = [];
  const sent = [];
  const socket = {readyState: 1, send: value => sent.push(JSON.parse(value)), close() { this.closed = true; }};
  const context = vm.createContext({
    ws: socket, WebSocket: {OPEN: 1}, Date: {now: () => now},
    document: {getElementById(id) { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); }},
    setInterval(fn) { tick = fn; return 1; }, clearInterval() { cleared++; },
    setRuntimeStatus: (...args) => status.push(args),
  });
  vm.runInContext(healthCode, context);
  return {context, nodes, status, socket, sent, cleared: () => cleared,
          advance(ms) { now += ms; tick(); }};
}

test('disconnect makes controls inert and explicitly marks displayed state as stale', () => {
  const app = dashboard();
  app.context.setCompanionConnected(false);
  assert.equal(app.nodes.get('dashboardContent').inert, true);
  assert.match(app.nodes.get('companionConnectionBanner').textContent, /disconnected.*stale/i);
  assert.deepEqual(app.status.at(-1), ['Companion disconnected', 'inactive']);
  app.context.setCompanionConnected(true);
  assert.equal(app.nodes.get('dashboardContent').inert, false);
  assert.equal(app.nodes.get('companionConnectionBanner').hidden, true);
});

test('a stalled backend disconnects after missed heartbeats', () => {
  const app = dashboard();
  app.context.startHeartbeat(app.socket);
  for (let i = 0; i < 7; i++) app.advance(1000);
  assert.equal(app.socket.closed, true);
  assert.equal(app.nodes.get('dashboardContent').inert, true);
  assert.match(app.nodes.get('companionConnectionBanner').textContent, /not responding/);
  assert.equal(app.sent[0].healthcheck, true);
});

test('resume from sleep allows a fresh health check before declaring a hang', () => {
  const app = dashboard();
  app.context.startHeartbeat(app.socket);
  app.advance(60000);
  assert.equal(app.socket.closed, undefined);
  assert.equal(app.sent.length, 1);
});

test('replaced sockets cannot send heartbeats and timers are cleaned up', () => {
  const app = dashboard();
  app.context.startHeartbeat(app.socket);
  app.context.ws = {readyState: 1};
  app.advance(1000);
  assert.equal(app.sent.length, 0);
  app.context.stopHeartbeat();
  assert.equal(app.cleared(), 1);
});

test('pending scaling commands are visible without claiming scaling is active', () => {
  const status = [];
  const context = vm.createContext({runtimeScalingActive: false, runtimeActiveProfile: null,
    renderProxyMenuAction() {}, renderRuntimeScalingStatus() {}, setRuntimeStatus: (...args) => status.push(args)});
  const runtimeCode = html.slice(html.indexOf('    function renderRuntime(msg)'), html.indexOf('    function activeProfileUsesProxy()'));
  vm.runInContext(runtimeCode, context);
  context.renderRuntime({isScalingActive: false, scalingControl: {state: 'pending', message: 'Waiting for scaling confirmation'}});
  assert.deepEqual(status.at(-1), ['Waiting for scaling confirmation', 'inactive']);
  assert.equal(context.runtimeScalingActive, false);
});

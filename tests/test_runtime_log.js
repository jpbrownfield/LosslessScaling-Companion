const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    let runtimeLogEntries = [];'), html.indexOf("    const processModal = document.getElementById('processModal');"));

function setup() {
  const nodes = new Map();
  function node() {
    return {children: [], dataset: {}, style: {}, value: '', hidden: false, scrollTop: 0, scrollHeight: 500,
      append(...items) { this.children.push(...items); }, appendChild(item) { this.children.push(item); },
      replaceChildren(fragment) { this.children = fragment.children; }, setAttribute() {}, click() {}};
  }
  const document = {hidden: false, createElement: node, createDocumentFragment: node,
    createTextNode: text => ({textContent: text}),
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }};
  document.getElementById('runtimeLogLevel').value = 'ALL';
  document.getElementById('runtimeLogFollow').checked = true;
  const sent = [];
  const copied = [];
  const context = vm.createContext({document, ws: {readyState: 1, send: value => sent.push(JSON.parse(value))},
    navigator: {clipboard: {async writeText(text) { copied.push(text); }}},
    WebSocket: {OPEN: 1}, setInterval() {}, setTimeout() {}, Date, Blob, URL});
  vm.runInContext(code, context);
  return {context, sent, copied, get: document.getElementById};
}

const entry = (id, level = 'INFO', message = 'scaling requested') => ({id, level, message, timestamp: new Date(Date.parse('2026-11-09T10:00:00Z') + id * 1000).toISOString(), source: 'LSCompanion.Automation'});
const batch = (entries, extra = {}) => ({entries, session: 'session-a', cursor: entries.at(-1)?.id || 0, reset: true, path: 'logs/companion.log', ...extra});

test('log filters, renders untrusted messages as text, and follows or preserves scroll', () => {
  const {context, get} = setup();
  context.receiveRuntimeLogs(batch([entry(1), entry(2, 'ERROR', '<img src=x onerror=alert(1)>'), entry(3, 'WARNING', 'lost focus')]));
  assert.equal(get('runtimeLogEntries').children.length, 3);
  assert.equal(get('runtimeLogEntries').children[1].children[1].children[1].textContent, '<img src=x onerror=alert(1)>');
  assert.equal(get('runtimeLogEntries').scrollTop, 500);
  get('runtimeLogFollow').checked = false;
  get('runtimeLogEntries').scrollTop = 80;
  get('runtimeLogLevel').value = 'WARNING';
  context.renderRuntimeLogs();
  assert.equal(get('runtimeLogEntries').children.length, 2);
  assert.equal(get('runtimeLogEntries').scrollTop, 80);
  get('runtimeLogSearch').value = 'lost focus';
  context.renderRuntimeLogs();
  assert.equal(get('runtimeLogEntries').children.length, 1);
  assert.equal(get('runtimeLogEntries').children[0].dataset.level, 'WARNING');
});

test('pause freezes in-flight batches and resume catches up; polling is bounded', () => {
  const {context, sent, get} = setup();
  context.receiveRuntimeLogs(batch([entry(1)]));
  context.requestRuntimeLogs();
  context.requestRuntimeLogs();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].since, 1);
  get('pauseRuntimeLogBtn').onclick.call(get('pauseRuntimeLogBtn'));
  context.receiveRuntimeLogs(batch([entry(2)], {reset: false}));
  assert.equal(get('runtimeLogEntries').children.length, 1);
  context.requestRuntimeLogs();
  assert.equal(sent.length, 1);
  get('pauseRuntimeLogBtn').onclick.call(get('pauseRuntimeLogBtn'));
  assert.equal(sent[1].since, 1);
  context.receiveRuntimeLogs(batch([entry(2)], {reset: false}));
  assert.equal(get('runtimeLogEntries').children.length, 2);
});

test('log handles expiration, session restart, disconnected status, and retention', () => {
  const {context, get} = setup();
  context.receiveRuntimeLogs(batch(Array.from({length: 1000}, (_, i) => entry(i + 1))));
  context.receiveRuntimeLogs(batch([entry(1001)], {reset: false, gap: true}));
  assert.equal(get('runtimeLogEntries').children.length, 1000);
  assert.match(get('runtimeLogStatus').textContent, /expired/);
  context.receiveRuntimeLogs(batch([entry(1)], {session: 'session-b'}));
  assert.equal(get('runtimeLogEntries').children.length, 1);
  assert.doesNotMatch(get('runtimeLogStatus').textContent, /expired/);
  vm.runInContext('ws.readyState = 3', context);
  context.requestRuntimeLogs();
  assert.match(get('runtimeLogStatus').textContent, /Disconnected/);
  context.receiveRuntimeLogs(batch([], {reset: false, session: 'session-b', cursor: 1}));
  assert.match(get('runtimeLogStatus').textContent, /Live/);
});

test('repeated or delayed incremental responses do not duplicate entries or rewind the cursor', () => {
  const {context, get, sent} = setup();
  context.receiveRuntimeLogs(batch([entry(1)]));
  const delta = batch([entry(2)], {reset: false});
  context.receiveRuntimeLogs(delta);
  context.receiveRuntimeLogs(delta);
  context.receiveRuntimeLogs(batch([entry(1)], {reset: false}));
  assert.equal(get('runtimeLogEntries').children.length, 2);
  context.requestRuntimeLogs();
  assert.equal(sent[0].since, 2);
});

test('expired entries disappear even when no new messages arrive', () => {
  const {context, get} = setup();
  context.receiveRuntimeLogs(batch([entry(1), entry(2)]));
  context.receiveRuntimeLogs(batch([], {reset: false, cursor: 2, oldestId: 2}));
  assert.equal(get('runtimeLogEntries').children.length, 1);
  assert.match(get('runtimeLogStatus').textContent, /1 shown \/ 1 retained/);
  context.receiveRuntimeLogs(batch([], {reset: false, cursor: 2, oldestId: 3}));
  assert.match(get('runtimeLogStatus').textContent, /0 shown \/ 0 retained/);
});

test('same-second events merge across batches and copying keeps every message and millisecond', async () => {
  const {context, get, copied} = setup();
  const first = {...entry(1), timestamp:'2026-11-09T10:00:00.100Z'};
  const second = {...entry(2, 'ERROR', 'Scaling not confirmed\nLast observation: none'), timestamp:'2026-11-09T10:00:00.999Z'};
  context.receiveRuntimeLogs(batch([first]));
  context.receiveRuntimeLogs(batch([second], {reset:false}));
  const group = get('runtimeLogEntries').children[0];
  assert.equal(get('runtimeLogEntries').children.length, 1);
  assert.equal(group.children.length, 3);
  assert.equal(group.dataset.level, 'ERROR');
  assert.match(group.children[0].children[0].textContent, /2 events/);
  await group.children[0].children[1].onclick();
  assert.match(copied[0], /UTC/);
  assert.match(copied[0], /\.100 \[INFO\]/);
  assert.match(copied[0], /\.999 \[ERROR\].*Scaling not confirmed\n  Last observation: none/);
  await get('copyRuntimeLogBtn').onclick.call(get('copyRuntimeLogBtn'));
  assert.equal(copied[1], copied[0]);
  get('runtimeLogLevel').value = 'ERROR';
  context.renderRuntimeLogs();
  assert.equal(get('runtimeLogEntries').children[0].children.length, 2);
  await get('copyRuntimeLogBtn').onclick.call(get('copyRuntimeLogBtn'));
  assert.doesNotMatch(copied[2], /\[INFO\]/);
});

test('timestamps convert UTC into the requested timezone with date rollover and daylight saving', () => {
  const {context} = setup();
  assert.equal(context.runtimeLogTimestamp('2026-11-09T01:00:00Z', 'America/New_York'), '2026-11-08 20:00:00 UTC-05:00');
  assert.equal(context.runtimeLogTimestamp('2026-07-09T01:00:00Z', 'America/New_York'), '2026-07-08 21:00:00 UTC-04:00');
});

test('scaling failures stay visible even while the log is paused', () => {
  const {context, get} = setup();
  get('pauseRuntimeLogBtn').onclick.call(get('pauseRuntimeLogBtn'));
  context.receiveRuntimeLogs(batch([], {scalingControl:{state:'error', message:'Lossless Scaling did not confirm activation'}}));
  assert.equal(get('runtimeScalingStatus').dataset.state, 'error');
  assert.match(get('runtimeScalingStatus').textContent, /did not confirm/);
  context.renderProxyMenuAction = () => {};
  const statuses = [];
  context.setRuntimeStatus = (...args) => statuses.push(args);
  vm.runInContext(html.slice(html.indexOf('    function renderRuntime(msg)'), html.indexOf('    function activeProfileUsesProxy()')), context);
  context.renderRuntime({losslessScalingRunning:true, scalingControl:{state:'error', message:'Scaling not confirmed'}});
  assert.deepEqual(statuses.at(-1), ['Scaling not confirmed', 'not-found']);
});

test('rebuild button sends the debug action and reports completion or disconnection', () => {
  const {context, get, sent} = setup();
  get('rebuildCloneBtn').onclick();
  assert.equal(sent[0].type, 'REBUILD_CLONED_INSTALLATION');
  assert.equal(get('rebuildCloneBtn').disabled, true);
  context.renderCloneRebuildStatus({busy:false, success:true, message:'Cloned installation rebuilt. Settings preserved.'});
  assert.equal(get('rebuildCloneBtn').disabled, false);
  assert.match(get('cloneRebuildStatus').textContent, /Settings preserved/);
  vm.runInContext('ws.readyState = 3', context);
  get('rebuildCloneBtn').onclick();
  assert.equal(sent.length, 1);
  assert.match(get('cloneRebuildStatus').textContent, /Reconnect/);
});

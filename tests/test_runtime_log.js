const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    let runtimeLogEntries = [];'), html.indexOf("    const processModal = document.getElementById('processModal');"));

function setup() {
  const nodes = new Map();
  function node() {
    return {children: [], dataset: {}, value: '', hidden: false, scrollTop: 0, scrollHeight: 500,
      append(...items) { this.children.push(...items); }, appendChild(item) { this.children.push(item); },
      replaceChildren(fragment) { this.children = fragment.children; }, setAttribute() {}, click() {}};
  }
  const document = {hidden: false, createElement: node, createDocumentFragment: node,
    createTextNode: text => ({textContent: text}),
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }};
  document.getElementById('runtimeLogLevel').value = 'ALL';
  document.getElementById('runtimeLogFollow').checked = true;
  const sent = [];
  const context = vm.createContext({document, ws: {readyState: 1, send: value => sent.push(JSON.parse(value))},
    WebSocket: {OPEN: 1}, setInterval() {}, setTimeout() {}, Date, Blob, URL});
  vm.runInContext(code, context);
  return {context, sent, get: document.getElementById};
}

const entry = (id, level = 'INFO', message = 'scaling requested') => ({id, level, message, timestamp: '2026-11-09T10:00:00.000+00:00', source: 'LSCompanion.Automation'});
const batch = (entries, extra = {}) => ({entries, session: 'session-a', cursor: entries.at(-1)?.id || 0, reset: true, path: 'logs/companion.log', ...extra});

test('log filters, renders untrusted messages as text, and follows or preserves scroll', () => {
  const {context, get} = setup();
  context.receiveRuntimeLogs(batch([entry(1), entry(2, 'ERROR', '<img src=x onerror=alert(1)>'), entry(3, 'WARNING', 'lost focus')]));
  assert.equal(get('runtimeLogEntries').children.length, 3);
  assert.equal(get('runtimeLogEntries').children[1].children[1].textContent, '<img src=x onerror=alert(1)>');
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

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    function renderAddonUpdateNotice('), html.indexOf('    function renderCompanionUpdate('));

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
}

function dashboard() {
  const nodes = new Map();
  const sent = [];
  const storage = new Map();
  const context = vm.createContext({
    addonUpdateItems: [], latestAddonVersions: {}, componentOperation: null,
    companionUpdateBusy: false,
    document: {getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Element('div')); return nodes.get(id); },
      createElement: tag => new Element(tag)},
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    updateAddonInstallButtons() {}, updateAddonDownloadLinks() {}, setGraphicsStatus() {},
    normalizedAddonVersion: value => String(value || '').replace(/^v/, ''),
    confirmDialog: async () => true,
    crypto: {randomUUID: () => 'test-operation'},
    ws: {readyState: 1, send: message => sent.push(JSON.parse(message))}, WebSocket: {OPEN: 1},
  });
  vm.runInContext(code, context);
  const updates = [{provider: 'ls-addon-manager', name: 'Echo', currentVersion: '1.0.5', latestVersion: '1.0.6'},
    {provider: 'reshade', name: 'ReShade', currentVersion: '6.7.0', latestVersion: '6.8.0'}];
  context.renderAddonUpdateNotice(updates, {});
  const rows = () => nodes.get('addonUpdateNotice').children;
  const button = index => rows()[index].children[1].children[1];
  return {context, nodes, sent, rows, button};
}

test('each component has its own row and Download starts that install directly', async () => {
  const {rows, button, sent} = dashboard();
  assert.equal(rows().length, 2);
  assert.equal(rows()[1].dataset.component, 'reshade');
  await button(1).onclick();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, 'INSTALL_LATEST_GRAPHICS_ADDON');
  assert.equal(sent[0].provider, 'reshade');
});

test('downloads show percent and disable all component/companion buttons', () => {
  const {context, button, nodes} = dashboard();
  context.renderComponentOperation({provider: 'ls-addon-manager', busy: true, phase: 'downloading', receivedBytes: 50, totalBytes: 100});
  assert.equal(button(0).textContent, 'Downloading 50%');
  assert.equal(button(0).disabled, true);
  assert.equal(button(1).disabled, true);
  assert.equal(nodes.get('downloadCompanionUpdateBtn').disabled, true);
  assert.equal(nodes.get('checkCompanionUpdateBtn').disabled, true);
});

test('repeated clicks cannot enqueue overlapping installs', async () => {
  const {context, sent} = dashboard();
  await context.installGraphicsAddon('reshade');
  await context.installGraphicsAddon('special-k');
  assert.equal(sent.length, 1);
});

test('verification keeps the lock and failure makes retry available', () => {
  const {context, button, rows} = dashboard();
  context.renderComponentOperation({provider: 'reshade', busy: true, phase: 'verifying'});
  assert.equal(button(1).textContent, 'Verifying...');
  assert.equal(button(0).disabled, true);
  context.renderComponentOperation({provider: 'reshade', busy: false, phase: 'failed', error: 'Hash mismatch'});
  assert.equal(button(1).disabled, false);
  assert.match(rows()[1].children[0].children[1].textContent, /Hash mismatch/);
});

test('dismissing a component does not dismiss the others', () => {
  const {rows} = dashboard();
  rows()[0].children[1].children[0].onclick();
  assert.equal(rows().length, 1);
  assert.equal(rows()[0].dataset.component, 'reshade');
});

test('unknown download sizes show received MB, not a fabricated percentage', () => {
  const {context} = dashboard();
  assert.equal(context.componentProgressLabel({phase: 'downloading', receivedBytes: 1048576}), 'Downloading 1.0 MB');
  assert.equal(context.componentProgressLabel({phase: 'downloading', receivedBytes: 200, totalBytes: 100}), 'Downloading 100%');
});

test('a completed older update does not disable the next release', () => {
  const {context, button} = dashboard();
  context.renderComponentOperation({provider: 'ls-addon-manager', busy: false, phase: 'complete', version: '1.0.5'});
  assert.equal(button(0).disabled, false);
  context.renderComponentOperation({provider: 'ls-addon-manager', busy: false, phase: 'complete', version: '1.0.6'});
  assert.equal(button(0).disabled, true);
});

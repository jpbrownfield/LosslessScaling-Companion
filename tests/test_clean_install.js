const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    let cleanInstallBusy = false;'),
  html.indexOf("    document.getElementById('revertAllChangesBtn').onclick ="));

test('dashboard scripts compile after wiring the reset controls', () => {
  for (const match of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
});

function setup(accepted = true) {
  const nodes = new Map();
  const document = {getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, {disabled: false, textContent: ''});
    return nodes.get(id);
  }};
  const values = new Map();
  const storage = {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key)};
  const sent = [], prompts = [];
  let reloads = 0;
  const ws = {readyState: 1, send: value => sent.push(JSON.parse(value))};
  const context = vm.createContext({document, ws, WebSocket: {OPEN: 1},
    generalSettingsSaveTimer: null, clearTimeout() {}, sessionStorage: storage, localStorage: storage,
    window: {location: {reload() { reloads++; }}},
    async confirmDialog(message, options) { prompts.push({message, options}); return accepted; }});
  vm.runInContext(code, context);
  return {context, sent, prompts, values, ws, get: document.getElementById, reloads: () => reloads};
}

test('orange clean install button appears above uninstall and confirmation describes all deleted state', async () => {
  assert.ok(html.indexOf('id="cleanInstallBtn"') < html.indexOf('id="revertAllChangesBtn"'));
  assert.match(html, /\.btn-clean-install \{ background: #f59e0b/);
  const ui = setup(false);
  await ui.get('cleanInstallBtn').onclick();
  assert.match(ui.prompts[0].message, /all general, add-on and private Lossless Scaling settings/);
  assert.match(ui.prompts[0].message, /delete the old cloned installation/);
  assert.equal(ui.sent.length, 0);
});

test('confirmed reset sends explicit authorization, prevents duplicates and reports failures', async () => {
  const ui = setup();
  await ui.get('cleanInstallBtn').onclick();
  assert.deepEqual(ui.sent, [{type: 'CLEAN_INSTALL', confirmCleanInstall: true}]);
  assert.equal(ui.get('cleanInstallBtn').disabled, true);
  assert.equal(ui.get('revertAllChangesBtn').disabled, true);
  await ui.get('cleanInstallBtn').onclick();
  assert.equal(ui.sent.length, 1);
  ui.context.renderCleanInstallStatus({busy: false, success: false, message: 'copy failed'});
  assert.equal(ui.get('cleanInstallBtn').disabled, false);
  assert.equal(ui.get('cleanInstallStatus').textContent, 'copy failed');
  assert.equal(ui.reloads(), 0);
});

test('disconnected reset never sends and completion reloads exactly once per operation', async () => {
  const ui = setup();
  ui.ws.readyState = 3;
  await ui.get('cleanInstallBtn').onclick();
  assert.equal(ui.sent.length, 0);
  assert.match(ui.get('cleanInstallStatus').textContent, /Reconnect/);
  const completed = {busy: false, settingsReset: true, operationId: 'reset-1', message: 'Reset complete'};
  ui.values.set('ignored-addon-update-versions', '["old"]');
  ui.context.renderCleanInstallStatus(completed);
  assert.equal(ui.reloads(), 1);
  assert.equal(ui.values.has('ignored-addon-update-versions'), false);
  ui.context.renderCleanInstallStatus(completed);
  assert.equal(ui.reloads(), 1, 'Status replay/reconnect must not cause an infinite reload loop');
  ui.context.renderCleanInstallStatus({...completed, operationId: 'reset-2'});
  assert.equal(ui.reloads(), 2);
});

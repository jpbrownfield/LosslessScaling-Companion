const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    let updateLockFocus = null;'), html.indexOf('    function renderComponentOperation('));

test('maintenance locks background controls, updates progress, and restores focus and prior inert states', () => {
  const nodes = new Map();
  const document = {body: {classList: {toggle() {}}}, activeElement: null, querySelectorAll: () => [get('main'), get('sidebar'), get('alreadyInert')], getElementById: get};
  function get(id) {
    if (!nodes.has(id)) nodes.set(id, {hidden: true, inert: false, isConnected: true,
      focus() { document.activeElement = this; }, removeAttribute(key) { delete this[key]; }});
    return nodes.get(id);
  }
  get('alreadyInert').inert = true;
  get('originalFocus').focus();
  const context = vm.createContext({document, componentProgressLabel: () => 'Downloading 50%'});
  vm.runInContext(code, context);
  context.renderUpdateLock({locked: true, phase: 'downloading', provider: 'companion', receivedBytes: 50, totalBytes: 100});
  assert.equal(get('updateLockOverlay').hidden, false);
  assert.equal(get('main').inert, true);
  assert.equal(document.activeElement, get('updateLockOverlay'));
  assert.equal(get('updateLockProgress').value, 50);
  context.renderUpdateLock({locked: true, phase: 'restarting'});
  assert.equal(get('updateLockTitle').textContent, 'Restarting Companion');
  assert.equal(get('updateLockProgress').value, undefined);
  context.renderUpdateLock({locked: false, phase: 'failed'});
  assert.equal(get('updateLockOverlay').hidden, true);
  assert.equal(get('main').inert, false);
  assert.equal(get('alreadyInert').inert, true);
  assert.equal(document.activeElement, get('originalFocus'));
});

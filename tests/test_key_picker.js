const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const script = html.slice(html.indexOf('    function shortcutFromKeyEvent('),
  html.lastIndexOf('    [', html.indexOf("'runAtStartup', 'overlayIndicatorEnabled'")));

function environment() {
  const inputs = new Map(), documentHandlers = new Map(), windowHandlers = new Map();
  const node = () => ({value: '', style: {}, children: [], handlers: new Map(), changes: 0,
    classList: {add() {}, remove() {}},
    contains(target) { return this === target || this.children.includes(target); },
    setAttribute() {}, addEventListener(name, fn) { this.handlers.set(name, fn); },
    appendChild(child) { this.children.push(child); },
    dispatchEvent(event) { if (event.type === 'change') this.changes++; }});
  const document = {getElementById(id) { if (!inputs.has(id)) {
    const input = node(); input.parentElement = node(); inputs.set(id, input);
  } return inputs.get(id); }, createElement: node,
    addEventListener(name, fn) { documentHandlers.set(name, fn); },
    removeEventListener(name, fn) { if (documentHandlers.get(name) === fn) documentHandlers.delete(name); }};
  const window = {addEventListener(name, fn) { windowHandlers.set(name, fn); },
    removeEventListener(name) { windowHandlers.delete(name); }};
  const context = vm.createContext({document, window, Event: class { constructor(type) { this.type = type; } }});
  vm.runInContext(html.slice(html.indexOf('    function renderOverrideHotkeySettings('),
    html.indexOf('    function renderGpuRoutingSettings(')), context);
  vm.runInContext(script, context);
  return {context, inputs, documentHandlers, windowHandlers};
}
const key = (value, extra = {}) => ({key: value, code: '', preventDefault() {}, stopImmediatePropagation() {}, ...extra});

test('key picker records named keys, modifiers, punctuation and supported navigation keys', () => {
  const {context} = environment();
  assert.equal(context.shortcutFromKeyEvent(key('Home', {ctrlKey: true})), 'ctrl+home');
  assert.equal(context.shortcutFromKeyEvent(key('Insert')), 'insert');
  assert.equal(context.shortcutFromKeyEvent(key('+', {code: 'Equal', shiftKey: true})), 'shift+=');
  assert.equal(context.shortcutFromKeyEvent(key('ArrowDown')), 'down');
  assert.equal(context.shortcutFromKeyEvent(key('Control')), null);
  assert.equal(context.shortcutFromKeyEvent(key('+', {code: 'NumpadAdd'})), null);
  assert.equal(context.shortcutFromKeyEvent(key('F8', {ctrlKey: true}), false), null);
});

test('Set key commits once, Escape cancels, Clear disables and blur ends capture', () => {
  const {inputs, documentHandlers, windowHandlers} = environment();
  const input = inputs.get('overlayProfileEditorHotkey');
  const [set, clear] = input.parentElement.children;
  assert.equal(input.readOnly, true);
  set.onclick();
  documentHandlers.get('keydown')(key('Delete'));
  assert.equal(input.value, 'delete');
  assert.equal(input.changes, 1);
  assert.equal(documentHandlers.has('keydown'), false);
  set.onclick(); documentHandlers.get('keydown')(key('Escape'));
  assert.equal(input.value, 'delete');
  assert.equal(input.changes, 1);
  set.onclick(); windowHandlers.get('blur')();
  assert.equal(documentHandlers.has('keydown'), false);
  set.onclick(); documentHandlers.get('pointerdown')({target: {}});
  assert.equal(documentHandlers.has('keydown'), false);
  clear.onclick();
  assert.equal(input.value, '');
  assert.equal(input.changes, 2);
  assert.equal(inputs.get('reshadeOverlayHotkey').parentElement.children.length, 1);
});

test('scaling shortcut records and serializes the full combination without modifier checkboxes', () => {
  const {inputs, documentHandlers, context} = environment();
  const input = inputs.get('overrideHotkeyKey');
  input.parentElement.children[0].onclick();
  documentHandlers.get('keydown')(key('Control', {ctrlKey: true}));
  assert.equal(input.changes, 0);
  documentHandlers.get('keydown')(key('F9', {ctrlKey: true, shiftKey: true}));
  assert.equal(input.value, 'ctrl+shift+f9');
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(overrideHotkeyPayload())', context)),
    {key: 'f9', modifiers: ['ctrl', 'shift']});
  input.parentElement.children[0].onclick();
  documentHandlers.get('keydown')(key('=', {code: 'Equal', metaKey: true}));
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(overrideHotkeyPayload())', context)),
    {key: '=', modifiers: ['win']});
  assert.equal(input.parentElement.children.length, 1);
});

test('ReShade records Ctrl/Alt/Shift combinations and ignores unsupported Win combinations', () => {
  const {inputs, documentHandlers} = environment();
  const input = inputs.get('reshadeOverlayHotkey');
  input.parentElement.children[0].onclick();
  documentHandlers.get('keydown')(key('F10', {metaKey: true}));
  assert.equal(input.changes, 0);
  documentHandlers.get('keydown')(key('F10', {ctrlKey: true, altKey: true}));
  assert.equal(input.value, 'ctrl+alt+f10');
  assert.equal(input.changes, 1);
});

test('disabled scaling picker cannot start recording', () => {
  const {inputs, documentHandlers} = environment();
  const input = inputs.get('overrideHotkeyKey');
  input.disabled = true;
  input.parentElement.children[0].onclick();
  assert.equal(documentHandlers.has('keydown'), false);
});

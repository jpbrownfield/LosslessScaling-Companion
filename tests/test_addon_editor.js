const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    function addonEditorChanged()'), html.indexOf('    function syncNativeControlDisplay('));

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.value = ''; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(name, callback) { this.events[name] = callback; }
  checkValidity() { return true; }
}

function editor() {
  const nodes = Object.fromEntries(['addonEditorCards', 'addonManagerFields', 'reshadeManagedProfile', 'profileId', 'addonEditorStatus'].map(id => [id, new Element('div')]));
  nodes.profileId.value = 'inactive-game-profile';
  const sent = [];
  const context = vm.createContext({
    document: {getElementById: id => nodes[id], createElement: tag => new Element(tag)},
    addonEditorCatalog: [{id: 'DLSS5NR01', name: 'DLSS 5', fields: [{key: 'workingScale', label: 'Resolution', type: 'number', default: '0.5'}]}],
    addonEditorDraft: {global: {custom: 17}, addons: {DLSS5NR01: {enabled: true}, Future: {numeric: 2, text: 'keep', nested: {flag: true}}}},
    addonEditorTimer: null, addonEditorGeneration: 0, editingOriginalProfile: {},
    setTimeout: () => 1, clearTimeout: () => {}, structuredClone,
    ws: {readyState: 1, send: data => sent.push(JSON.parse(data))}, WebSocket: {OPEN: 1},
  });
  vm.runInContext(code, context);
  context.renderAddonEditor();
  function find(label) {
    const walk = node => node['aria-label'] === label ? node : node.children.map(walk).find(Boolean);
    return walk(nodes.addonEditorCards) || walk(nodes.addonManagerFields);
  }
  return {context, nodes, sent, find};
}

test('inactive profile controls opt into DLSS and preserve unrelated JSON settings', () => {
  const {context, sent, find} = editor();
  assert.equal(context.addonEditorDraft.addons.DLSS5NR01._enabled, false);
  const enabled = find('Enable DLSS 5');
  enabled.checked = true;
  enabled.events.change();
  context.saveAddonEditor();
  assert.equal(sent[0].profileId, 'inactive-game-profile');
  assert.equal(sent[0].config.addons.DLSS5NR01._enabled, true);
  assert.equal(sent[0].config.addons.Future.nested.flag, true);
  assert.equal(sent[0].config.global.custom, 17);
  assert.equal(sent[0].config.addons.DLSS5NR01.workingScale, undefined);
});

test('known numeric settings use Echo strings and unknown numeric values retain JSON types', () => {
  const {context, sent, find} = editor();
  const resolution = find('Resolution');
  resolution.value = '0.6';
  resolution.events.change();
  const numeric = find('numeric');
  numeric.value = '3';
  numeric.events.change();
  context.saveAddonEditor();
  assert.equal(sent[0].config.addons.DLSS5NR01.workingScale, '0.6');
  assert.equal(sent[0].config.addons.Future.numeric, 3);
  assert.equal(sent[0].config.addons.Future.text, 'keep');
});

test('the entire dashboard script parses', () => {
  new vm.Script(html.slice(html.indexOf('<script>') + 8, html.indexOf('</script>')));
});

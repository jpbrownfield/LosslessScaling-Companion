const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const start = html.indexOf('    function renderReshadeMonitorNits(');
const end = html.indexOf('    function normalizedAddonVersion(', start);

test('monitor controls use identities and preserve disconnected overrides when clearing an entry', () => {
  const all = [];
  let saves = 0;
  class Element {
    constructor(tag) { this.tag = tag; this.attributes = {}; this.dataset = {}; this.children = []; this._value = ''; }
    set value(value) { this._value = String(value); }
    get value() { return this._value; }
    appendChild(child) { this.children.push(child); }
    replaceChildren() { this.children = []; }
    setAttribute(key, value) { this.attributes[key] = value; }
    reportValidity() { return this.valid !== false; }
    addEventListener(_, fn) { this.change = fn; }
  }
  const container = new Element();
  const defaultRow = new Element();
  const context = vm.createContext({
    controlSettings: {reshadeHdr: {monitorSdrWhiteNits: {a: 600, b: 300, disconnected: 700}}},
    scheduleGeneralSettingsSave() { saves++; },
    document: {
      getElementById: id => id === 'reshadeDefaultNitsRow' ? defaultRow : container,
      createElement(tag) { const element = new Element(tag); all.push(element); return element; },
      querySelectorAll: () => all.filter(element => element.dataset.reshadeMonitorId),
    },
  });
  vm.runInContext(html.slice(start, end), context);
  vm.runInContext(`renderReshadeMonitorNits({sdrWhiteNits: 400,
    monitorSdrWhiteNits: {a: 600, b: 300},
    monitors: [{id: 'a', name: 'Same model'}, {id: 'b', name: '?\\u0000\\ufffd', deviceName: 'DISPLAY2'}, {id: 'b', name: 'Duplicate'}]})`, context);
  const inputs = all.filter(element => element.dataset.reshadeMonitorId);
  assert.equal(inputs.length, 2);
  assert.equal(defaultRow.hidden, true);
  assert.equal(container.children[0].tag, 'table');
  assert.match(inputs[1].attributes['aria-label'], /^DISPLAY2/);
  assert.doesNotMatch(inputs[0].attributes['aria-label'], /\?/);
  assert.equal(inputs[0].placeholder, 'Default: 400');
  inputs[0].value = 500;
  inputs[1].value = '';
  inputs[0].change();
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(reshadeMonitorNitsPayload())', context)),
    {a: 500, disconnected: 700});
  assert.equal(saves, 1);
  inputs[0].valid = false;
  inputs[0].change();
  assert.equal(saves, 1, 'invalid values must not trigger a save');
  context.renderReshadeMonitorNits({monitors: [{name: 'No identity'}]});
  assert.equal(defaultRow.hidden, false);
  assert.match(container.children[0].textContent, /No monitors detected/);
});

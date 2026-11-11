const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const start = html.indexOf("    document.getElementById('displayCommanderReflex').addEventListener");
const end = html.indexOf("    document.getElementById('browseTargetProgramBtn')", start);

test('Reflex enables Commander, parent off clears Reflex, and Special K takes ownership', () => {
  const elements = new Map();
  let saves = 0;
  const get = id => {
    if (!elements.has(id)) elements.set(id, {checked: false, addEventListener(_, fn) { this.change = fn; }});
    return elements.get(id);
  };
  vm.runInNewContext(html.slice(start, end), {document: {getElementById: get},
    renderAddonEditor() {}, scheduleProfileAutosave() { saves++; }});
  get('specialKReflex').checked = true;
  get('displayCommanderReflex').checked = true;
  get('displayCommanderReflex').change({target: get('displayCommanderReflex')});
  assert.equal(get('displayCommanderEnabled').checked, true);
  assert.equal(get('specialKReflex').checked, false);
  get('displayCommanderEnabled').checked = false;
  get('displayCommanderEnabled').change({target: get('displayCommanderEnabled')});
  assert.equal(get('displayCommanderReflex').checked, false);
  get('displayCommanderReflex').checked = true;
  get('specialKReflex').checked = true;
  get('specialKReflex').change({target: get('specialKReflex')});
  assert.equal(get('displayCommanderReflex').checked, false);
  assert.equal(saves, 3);
});

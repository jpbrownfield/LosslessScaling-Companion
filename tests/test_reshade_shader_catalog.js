const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    function updateShaderPackSelectionState('), html.indexOf('    function openReshadeProfileModal('));

function editor() {
  const nodes = {reshadeShaderSearch: {value: ''}, reshadeSelectedOnly: {checked: false}, reshadeShaderSummary: {}};
  const groups = [];
  nodes.reshadeShaderCatalog = {
    set innerHTML(value) {
      this.markup = value;
      groups.length = 0;
      for (const match of value.matchAll(/<tbody data-pack-index="(\d+)">([\s\S]*?)<\/tbody>/g)) {
        const boxes = [...match[2].matchAll(/data-shader value="([^"]*)"[^>]*?(checked)?\s*>/g)]
          .map(box => ({value: box[1], checked: !!box[2]}));
        const toggle = {};
        const count = {};
        groups.push({dataset: {packIndex: match[1]}, boxes, toggle, count,
          querySelector: selector => selector === '.reshade-pack-toggle' ? toggle : count,
          querySelectorAll: () => boxes});
      }
    },
    querySelectorAll: selector => selector === '[data-pack-index]' ? groups : groups.flatMap(group => group.boxes),
  };
  let saves = 0;
  const context = vm.createContext({
    document: {getElementById: id => nodes[id]},
    reshadeCatalog: [
      {id: 'a', name: 'Clarity Pack', shaders: ['Sharp.fx', 'Bloom.fx', 'folder/Sharp.fx']},
      {id: 'b', name: 'Other Pack', shaders: ['Sharp.fx', '<unsafe>.fx']},
    ],
    reshadeShaderSelection: {a: new Set(['Bloom.fx']), absent: new Set(['Saved.fx'])},
    escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
    scheduleReshadeAutosave: () => saves++,
  });
  vm.runInContext(code, context);
  return {context, nodes, groups, saves: () => saves};
}

test('search combines shader and pack terms, includes saved missing files, and selected-only preserves selections', () => {
  const {context} = editor();
  const result = query => JSON.parse(JSON.stringify(context.filteredReshadeShaderPacks(query, false)));
  assert.deepEqual(result('clarity sharp').map(pack => pack.visibleShaders), [['folder/Sharp.fx', 'Sharp.fx']]);
  assert.equal(result('SHARP').length, 2);
  assert.equal(result('saved')[0].id, 'absent');
  assert.deepEqual(JSON.parse(JSON.stringify(context.filteredReshadeShaderPacks('', true))).map(pack => pack.visibleShaders), [['Bloom.fx'], ['Saved.fx']]);
  context.reshadeShaderSelection.a.add('Removed.fx');
  assert.deepEqual(result('removed')[0].visibleShaders, ['Removed.fx']);
  assert.equal(result('no such shader').length, 0);
});

test('group selection affects only search results and duplicate filenames remain independent by pack', () => {
  const {context, nodes, groups, saves} = editor();
  nodes.reshadeShaderSearch.value = 'clarity sharp';
  context.renderReshadeShaderCatalog();
  assert.equal(groups[0].boxes.length, 2, 'shader rows are available without expanding a pack');
  groups[0].toggle.onchange({target: {checked: true}});
  assert.equal(context.reshadeShaderSelection.a.size, 3, 'hidden Bloom selection survives');
  assert.equal(context.reshadeShaderSelection.b, undefined);
  assert.equal(groups[0].toggle.checked, true);
  assert.equal(saves(), 1);
  groups[0].boxes[0].checked = false;
  groups[0].boxes[0].onchange();
  assert.equal(groups[0].toggle.indeterminate, true);
  groups[0].toggle.onchange({target: {checked: false}});
  assert.deepEqual([...context.reshadeShaderSelection.a], ['Bloom.fx']);
  assert.deepEqual([...context.reshadeShaderSelection.absent], ['Saved.fx']);
  assert.match(nodes.reshadeShaderSummary.textContent, /2 selected \/ 2 shown/);
});

test('selected-only removal refreshes results, escapes filenames, and filtering does not save', () => {
  const {context, nodes, groups, saves} = editor();
  context.renderReshadeShaderCatalog();
  assert.match(nodes.reshadeShaderCatalog.markup, /&lt;unsafe&gt;\.fx/);
  nodes.reshadeSelectedOnly.checked = true;
  context.renderReshadeShaderCatalog();
  assert.equal(saves(), 0);
  const absent = groups.find(group => group.boxes.some(box => box.value === 'Saved.fx'));
  absent.toggle.onchange({target: {checked: false}});
  assert.equal(context.reshadeShaderSelection.absent, undefined);
  assert.equal(groups.length, 1);
  assert.match(nodes.reshadeShaderSummary.textContent, /1 selected \/ 1 shown/);
});

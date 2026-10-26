const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    function profileUpscalerChoice('), html.indexOf('    function syncNativeControlDisplay('));
const nativePayloadCode = html.slice(html.indexOf('    function nativeSettingsPayload()'), html.indexOf('    function setProfileEditorOpen('));

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
  const nodes = Object.fromEntries(['addonEditorCards', 'addonManagerFields', 'reshadeManagedProfile', 'profileId', 'addonEditorStatus', 'nativeScalingType', 'profileScalingTypeHint', 'nativePreferredGpu', 'nativeOutputDisplay'].map(id => [id, new Element('div')]));
  nodes.profileId.value = 'inactive-game-profile';
  nodes.nativeScalingType.value = 'NIS';
  nodes.nativeScalingType.dataset = {nativeKey: 'ScalingType', nativeAvailable: 'true'};
  const sent = [];
  const profileSaves = [];
  const context = vm.createContext({
    document: {getElementById: id => nodes[id], createElement: tag => new Element(tag),
      querySelectorAll: selector => selector === '[data-native-key]' ? [nodes.nativeScalingType] : []},
    addonEditorCatalog: [{id: 'DLSS5NR01', name: 'DLSS 5', fields: [{key: 'workingScale', label: 'Resolution', type: 'number', default: '0.5'}]},
      {id: 'DLSS4DLAA', name: 'DLSS Upscaler', fields: []}, {id: 'FSR3UPSC', name: 'FSR Upscaler', fields: []}],
    addonEditorDraft: {global: {custom: 17}, addons: {DLSS5NR01: {enabled: true}, Future: {numeric: 2, text: 'keep', nested: {flag: true}}}},
    addonEditorTimer: null, addonEditorGeneration: 0, editingOriginalProfile: {},
    addonScalingSavePending: false, controlSettings: {addonManagerEnabled: true},
    scheduleProfileAutosave: delay => profileSaves.push(delay),
    setTimeout: () => 1, clearTimeout: () => {}, structuredClone,
    ws: {readyState: 1, send: data => sent.push(JSON.parse(data))}, WebSocket: {OPEN: 1},
  });
  vm.runInContext(code, context);
  vm.runInContext(nativePayloadCode, context);
  context.renderAddonEditor();
  function find(label) {
    const walk = node => node['aria-label'] === label ? node : node.children.map(walk).find(Boolean);
    return walk(nodes.addonEditorCards) || walk(nodes.addonManagerFields);
  }
  return {context, nodes, sent, find, profileSaves};
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

test('DLSS scaling selects native NIS, enables its add-on and disables competing upscalers only', () => {
  const {context, nodes, sent, profileSaves} = editor();
  const addons = context.addonEditorDraft.addons;
  addons.DLSS4DLAA = {_enabled: false, sharpen: '0.7', futureSetting: 'keep'};
  addons.FSR3UPSC = {_enabled: true, frameGen: '1'};
  addons.XESSUPSC = {_enabled: true};
  addons.VSRUPSC = {_enabled: true};
  addons.DLSS5NR01._enabled = true;
  context.changeProfileScalingChoice('EchoDLSS');
  assert.equal(nodes.nativeScalingType.value, 'EchoDLSS');
  assert.equal(context.nativeSettingsPayload().ScalingType, 'NIS');
  assert.equal(addons.DLSS4DLAA._enabled, true);
  assert.equal(addons.DLSS4DLAA.futureSetting, 'keep');
  assert.equal(addons.DLSS4DLAA.sharpen, '0.7');
  for (const id of ['FSR3UPSC', 'XESSUPSC', 'VSRUPSC']) assert.equal(addons[id]._enabled, false);
  assert.equal(addons.FSR3UPSC.frameGen, '1');
  assert.equal(addons.DLSS5NR01._enabled, true);
  assert.equal(addons.Future.nested.flag, true);
  assert.equal(context.addonScalingSavePending, true);
  context.saveAddonEditor();
  assert.equal(sent.length, 0, 'do not save the add-on config separately from native NIS');
  assert.ok(profileSaves.length > 0);
});

test('switching DLSS to Echo FSR and then native FSR preserves a coherent native/add-on pair', () => {
  const {context, nodes} = editor();
  context.changeProfileScalingChoice('EchoDLSS');
  context.changeProfileScalingChoice('EchoFSR');
  assert.equal(nodes.nativeScalingType.value, 'EchoFSR');
  assert.equal(context.nativeSettingsPayload().ScalingType, 'NIS');
  assert.equal(context.addonEditorDraft.addons.DLSS4DLAA._enabled, false);
  assert.equal(context.addonEditorDraft.addons.FSR3UPSC._enabled, true);
  context.changeProfileScalingChoice('FSR');
  assert.equal(nodes.nativeScalingType.value, 'FSR');
  assert.equal(context.nativeSettingsPayload().ScalingType, 'FSR');
  assert.equal(context.addonEditorDraft.addons.FSR3UPSC._enabled, false);
});

test('reopening NIS profiles recognizes enabled Echo modes but does not infer opt-in from legacy settings', () => {
  const {context} = editor();
  assert.equal(context.profileUpscalerChoice('NIS', {DLSS4DLAA: {_enabled: true}}), 'EchoDLSS');
  assert.equal(context.profileUpscalerChoice('NIS', {FSR3UPSC: {_enabled: true}}), 'EchoFSR');
  assert.equal(context.profileUpscalerChoice('NIS', {DLSS4DLAA: {enabled: true}}), 'NIS');
  assert.equal(context.profileUpscalerChoice('LS1', {DLSS4DLAA: {_enabled: true}}), 'LS1');
});

test('add-on card toggles stay synchronized with the scaling dropdown and compound save', () => {
  const {context, nodes, find, sent} = editor();
  const enabled = find('Enable FSR Upscaler');
  enabled.checked = true;
  enabled.events.change();
  assert.equal(nodes.nativeScalingType.value, 'EchoFSR');
  assert.equal(context.nativeSettingsPayload().ScalingType, 'NIS');
  context.saveAddonEditor();
  assert.equal(sent.length, 0);
  const disabled = find('Enable FSR Upscaler');
  disabled.checked = false;
  disabled.events.change();
  assert.equal(nodes.nativeScalingType.value, 'NIS');
});

test('missing manager or upscaler is explained without enabling the global add-on toggle', () => {
  const {context, nodes} = editor();
  context.controlSettings.addonManagerEnabled = false;
  context.changeProfileScalingChoice('EchoDLSS');
  assert.match(nodes.profileScalingTypeHint.textContent, /General Settings/);
  assert.equal(context.controlSettings.addonManagerEnabled, false);
  context.controlSettings.addonManagerEnabled = true;
  context.addonEditorCatalog.find(addon => addon.id === 'DLSS4DLAA').installed = false;
  context.renderProfileScalingChoice();
  assert.match(nodes.profileScalingTypeHint.textContent, /not installed/);
});

test('GPU saves use the identity of the displayed option even after inventory reordering', () => {
  const {context, nodes} = editor();
  nodes.nativePreferredGpu.value = '1';
  nodes.nativePreferredGpu.selectedOptions = [{dataset: {deviceId: 'nvidia-old', deviceName: 'NVIDIA RTX'}}];
  nodes.nativeOutputDisplay.value = '2';
  nodes.nativeOutputDisplay.selectedOptions = [{dataset: {deviceId: 'monitor-id', deviceName: 'Gaming monitor'}}];
  context.controlSettings.gpuInventory = {gpus: [{lsGpuId: 1, deviceId: 'amd', name: 'AMD'}]};
  const payload = context.profileGpuSelectionPayload();
  assert.equal(payload.preferred_gpu_device_id, 'nvidia-old');
  assert.equal(payload.preferred_gpu_name, 'NVIDIA RTX');
  assert.equal(payload.output_display_device_id, 'monitor-id');
  nodes.nativePreferredGpu.value = '0';
  assert.equal(context.profileGpuSelectionPayload().preferred_gpu_device_id, null);
});

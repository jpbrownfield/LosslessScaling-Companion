const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    function profileUpscalerChoice('), html.indexOf('    function syncNativeControlDisplay('));
const nativePayloadCode = html.slice(html.indexOf('    function nativeSettingsPayload()'), html.indexOf('    function setProfileEditorOpen('));

class Element {
  constructor(tag) { this.tag = tag; this.tagName = tag.toUpperCase(); this.children = []; this.events = {}; this.style = {}; this.value = ''; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  setAttribute(key, value) { this[key] = value; }
  getAttribute(key) { return this[key] ?? null; }
  addEventListener(name, callback) { this.events[name] = callback; }
  checkValidity() { return true; }
}

function editor() {
  const nodes = Object.fromEntries(['addonScalingFields', 'nativeScalingSharpnessRow', 'addonEditorCards', 'addonManagerFields', 'addonEditorSearch', 'addonEditorSummary', 'addonHostNotice', 'reshadeManagedProfile', 'displayCommanderEnabled', 'displayCommanderReflex', 'profileId', 'addonEditorStatus', 'nativeScalingType', 'nativeFrameGenerationType', 'nativeCaptureApi', 'profileScalingTypeHint', 'nativePreferredGpu', 'nativeOutputDisplay'].map(id => [id, new Element('div')]));
  nodes.profileId.value = 'inactive-game-profile';
  nodes.nativeScalingType.value = 'NIS';
  nodes.nativeScalingType.dataset = {nativeKey: 'ScalingType', nativeAvailable: 'true'};
  nodes.nativeFrameGenerationType.value = 'OFF';
  nodes.nativeCaptureApi.value = 'DXGI';
  const sent = [];
  const profileSaves = [];
  const context = vm.createContext({
    notificationState: {repairSession: false},
    document: {getElementById: id => nodes[id], createElement: tag => new Element(tag),
      querySelector: selector => selector === '[data-native-key="CaptureApi"]' ? nodes.nativeCaptureApi : null,
      querySelectorAll: selector => {
        if (selector === '[data-native-key]') return [nodes.nativeScalingType];
        const attribute = /^\[([^\]]+)\]$/.exec(selector)?.[1];
        const matches = [];
        const walk = node => { if (attribute && node[attribute] != null) matches.push(node); node.children.forEach(walk); };
        Object.values(nodes).forEach(walk);
        return matches;
      }},
    addonEditorCatalog: [{id: 'DLSSNR02', name: 'DLSS 5', fields: [{key: 'scalePercent', label: 'Resolution', type: 'number', default: '50'}]},
      {id: 'DLSS4DLAA', name: 'DLSS Upscaler', fields: [
        {key: 'dlaaPreset', label: 'DLSS upscaling preset', type: 'select', options: [['12', 'L'], ['13', 'M']]},
        {key: 'companionSm86Mode', label: 'SM86 optimized Super Resolution', type: 'select',
          options: [['Off', 'Off'], ['L-INT8', 'L-INT8'], ['M-INT8', 'M-INT8']]}]},
      {id: 'FSR3UPSC', name: 'FSR Upscaler', fields: []}, {id: 'XESSUPSC', name: 'XeSS Upscaler', fields: []}],
    addonEditorDraft: {global: {custom: 17}, addons: {DLSSNR02: {enabled: true}, Future: {numeric: 2, text: 'keep', nested: {flag: true}}}},
    addonEditorTimer: null, addonEditorGeneration: 0, editingOriginalProfile: {},
    addonScalingSavePending: false, controlSettings: {addonManagerEnabled: true},
    scheduleProfileAutosave: delay => profileSaves.push(delay),
    renderFrameGenerationModeSettings: () => {}, renderProxyMenuAction: () => {},
    setTimeout: () => 1, clearTimeout: () => {}, structuredClone,
    ws: {readyState: 1, send: data => sent.push(JSON.parse(data))}, WebSocket: {OPEN: 1},
  });
  vm.runInContext(code, context);
  vm.runInContext(nativePayloadCode, context);
  context.renderAddonEditor();
  function find(label) {
    const walk = node => node['aria-label'] === label ? node : node.children.map(walk).find(Boolean);
    return walk(nodes.addonEditorCards) || walk(nodes.addonManagerFields) || walk(nodes.addonScalingFields);
  }
  function savedJson() {
    const values = [];
    const walk = node => {
      if (node.tag === 'pre') values.push(JSON.parse(node.textContent));
      node.children.forEach(walk);
    };
    walk(nodes.addonEditorCards);
    walk(nodes.addonManagerFields);
    return values;
  }
  return {context, nodes, sent, find, savedJson, profileSaves};
}

test('inactive profile controls opt into DLSS and preserve unrelated JSON settings', () => {
  const {context, sent, find, profileSaves, nodes} = editor();
  assert.equal(context.addonEditorDraft.addons.DLSSNR02._enabled, false);
  const enabled = find('Enable DLSS 5');
  enabled.checked = true;
  enabled.events.change();
  context.saveAddonEditor();
  assert.equal(sent.length, 0, 'NR and capture settings must use the compound profile save');
  assert.equal(context.addonScalingSavePending, true);
  assert.ok(profileSaves.length > 0);
  assert.equal(nodes.nativeCaptureApi.value, 'WGC');
  const config = context.editingOriginalProfile.addon_config;
  assert.equal(config.addons.DLSSNR02._enabled, true);
  assert.equal(config.addons.Future.nested.flag, true);
  assert.equal(config.global.custom, 17);
  assert.equal(config.addons.DLSSNR02.scalePercent, undefined);
});

test('known numeric settings use upstream strings and unknown numeric values are preserved', () => {
  const {context, sent, find, savedJson} = editor();
  const resolution = find('Resolution');
  resolution.value = '60';
  resolution.events.change();
  assert.equal(find('numeric'), undefined, 'unknown values are compatibility data, not editable controls');
  assert.equal(context.addonEditorDraft.addons.Future.numeric, 2);
  context.saveAddonEditor();
  assert.equal(sent[0].config.addons.DLSSNR02.scalePercent, '60');
  assert.equal(sent[0].config.addons.Future.numeric, 2);
  assert.equal(sent[0].config.addons.Future.text, 'keep');
});

test('INT8 selection pins L/M, locks its standard preset and saves independently of NR', () => {
  const {context, find, sent} = editor();
  context.addonEditorDraft.addons.DLSSNR02.preset = '2';
  context.addonEditorDraft.addons.DLSS4DLAA = {_enabled: true};
  context.renderAddonEditor();
  for (const [mode, preset] of [['L-INT8', '12'], ['M-INT8', '13']]) {
    const backend = find('SM86 optimized Super Resolution');
    backend.value = mode;
    backend.events.change();
    const standard = find('DLSS upscaling preset');
    assert.equal(standard.value, preset);
    assert.equal(standard.disabled, true);
    context.saveAddonEditor();
    const saved = sent.at(-1).config.addons;
    assert.equal(saved.DLSS4DLAA.companionSm86Mode, mode);
    assert.equal(saved.DLSS4DLAA.dlaaPreset, preset);
    assert.equal(saved.DLSS4DLAA.scalerPerGame, '0');
    assert.equal(saved.DLSSNR02.preset, '2');
    assert.equal(saved.DLSSNR02.companionSm86Mode, undefined);
  }
  const backend = find('SM86 optimized Super Resolution');
  backend.value = 'Off';
  backend.events.change();
  const standard = find('DLSS upscaling preset');
  assert.equal(standard.disabled, false);
  standard.value = '';
  standard.events.change();
  context.saveAddonEditor();
  assert.equal(sent.at(-1).config.addons.DLSS4DLAA.dlaaPreset, undefined);
  assert.equal(sent.at(-1).config.addons.DLSS4DLAA.companionSm86Mode, 'Off');
});

test('the entire dashboard script parses', () => {
  new vm.Script(html.slice(html.indexOf('<script>') + 8, html.indexOf('</script>')));
});

test('DLSS scaling fields respect inventory and advanced backend controls stay in the panel', () => {
  const {context, nodes, find} = editor();
  context.addonEditorCatalog[0].fields.push({key: 'amdModelPath', label: 'AMD model', type: 'text'});
  context.addonEditorDraft.addons.DLSS4DLAA = {_enabled: true};
  context.renderAddonEditor();
  assert.equal(find('AMD model'), undefined);
  assert.equal(find('Enable DLSS Upscaler'), undefined);
  assert.equal(find('Enable FSR Upscaler'), undefined);
  assert.ok(find('Enable DLSS 5'));
  assert.ok(find('DLSS upscaling preset'));
  assert.equal(context.addonVendorDetected('NVIDIA'), false);
  context.controlSettings.gpuInventory = {gpus: [{deviceId: 'PCI\\VEN_10DE'}]};
  assert.equal(context.addonVendorDetected('NVIDIA'), true);
});

test('hidden managed controls do not reappear as additional saved settings', () => {
  const {context, find} = editor();
  context.addonEditorCatalog[0].fields.push({key: 'apply', label: 'Comparison output', hidden: true});
  context.addonEditorDraft.addons.DLSSNR02.apply = '0';
  context.renderAddonEditor();
  assert.equal(find('Comparison output'), undefined);
  assert.equal(find('apply'), undefined);
  assert.ok(find('Resolution'));
});

test('host housekeeping fields stay hidden while unknown host settings survive', () => {
  const {context, find, savedJson} = editor();
  context.addonEditorDraft.global = {auto_load: false, ui: {open_on_start: true, customUi: 'keep'}};
  context.renderAddonEditor();
  assert.equal(find('auto load'), undefined);
  assert.equal(find('open on start'), undefined);
  assert.equal(find('custom Ui'), undefined);
  assert.equal(context.addonEditorDraft.global.ui.customUi, 'keep');
});

test('live scaling status immediately follows Scale Now inside the sidebar', () => {
  const footer = html.slice(html.indexOf('<div class="sidebar-footer">'), html.indexOf('<main id="mainContent"'));
  assert.ok(footer.indexOf('id="statusPill"') > footer.indexOf('id="scaleNowBtn"'));
  assert.equal((html.match(/id="statusPill"/g) || []).length, 1);
  assert.doesNotMatch(footer, /Switch to your app/);
});

test('addon toolbar binds and tolerates clicks before the connection opens', () => {
  const nodes = {};
  const context = vm.createContext({ws: null, WebSocket: {OPEN: 1},
    document: {getElementById: id => nodes[id] ||= {}}});
  vm.runInContext(html.slice(html.indexOf("    document.getElementById('refreshGraphicsBtn').onclick"),
    html.indexOf('    function downloadBenchmarkTool(')), context);
  nodes.refreshGraphicsBtn.onclick();
  nodes.openProxyMenuBtn.onclick();
  const sent = [];
  context.ws = {readyState: 1, send: message => sent.push(JSON.parse(message).type)};
  nodes.refreshGraphicsBtn.onclick();
  assert.deepEqual(sent, ['GET_GRAPHICS_STATUS', 'GET_ADDON_RUNTIME_STATUS']);
});

test('DLSS scaling selects native NIS, enables its add-on and disables competing upscalers only', () => {
  const {context, nodes, sent, profileSaves} = editor();
  const addons = context.addonEditorDraft.addons;
  addons.DLSS4DLAA = {_enabled: false, sharpen: '0.7', futureSetting: 'keep'};
  addons.FSR3UPSC = {_enabled: true, frameGen: '1'};
  addons.XESSUPSC = {_enabled: true};
  addons.VSRUPSC = {_enabled: true};
  addons.DLSSNR02._enabled = true;
  context.changeProfileScalingChoice('AddonDLSS');
  assert.equal(nodes.nativeScalingType.value, 'AddonDLSS');
  assert.equal(context.nativeSettingsPayload().ScalingType, 'NIS');
  assert.equal(addons.DLSS4DLAA._enabled, true);
  assert.equal(addons.DLSS4DLAA.futureSetting, 'keep');
  assert.equal(addons.DLSS4DLAA.sharpen, '0.7');
  for (const id of ['FSR3UPSC', 'XESSUPSC', 'VSRUPSC']) assert.equal(addons[id]._enabled, false);
  assert.equal(addons.FSR3UPSC.frameGen, '1');
  assert.equal(addons.DLSSNR02._enabled, true);
  assert.equal(addons.Future.nested.flag, true);
  assert.equal(context.addonScalingSavePending, true);
  context.saveAddonEditor();
  assert.equal(sent.length, 0, 'do not save the add-on config separately from native NIS');
  assert.ok(profileSaves.length > 0);
});

test('switching DLSS to addon FSR and then native FSR preserves a coherent native/add-on pair', () => {
  const {context, nodes} = editor();
  context.changeProfileScalingChoice('AddonDLSS');
  context.changeProfileScalingChoice('AddonFSR');
  assert.equal(nodes.nativeScalingType.value, 'AddonFSR');
  assert.equal(context.nativeSettingsPayload().ScalingType, 'NIS');
  assert.equal(context.addonEditorDraft.addons.DLSS4DLAA._enabled, false);
  assert.equal(context.addonEditorDraft.addons.FSR3UPSC._enabled, true);
  context.changeProfileScalingChoice('FSR');
  assert.equal(nodes.nativeScalingType.value, 'FSR');
  assert.equal(context.nativeSettingsPayload().ScalingType, 'FSR');
  assert.equal(context.addonEditorDraft.addons.FSR3UPSC._enabled, false);
});

test('reopening NIS profiles recognizes enabled addon modes but does not infer opt-in from legacy settings', () => {
  const {context} = editor();
  assert.equal(context.profileUpscalerChoice('NIS', {DLSS4DLAA: {_enabled: true}}), 'AddonDLSS');
  assert.equal(context.profileUpscalerChoice('NIS', {FSR3UPSC: {_enabled: true}}), 'AddonFSR');
  assert.equal(context.profileUpscalerChoice('NIS', {DLSS4DLAA: {enabled: true}}), 'NIS');
  assert.equal(context.profileUpscalerChoice('LS1', {DLSS4DLAA: {_enabled: true}}), 'LS1');
});

test('XeSS is selected through Scaling with no duplicate upscaler toggles', () => {
  const {context, nodes, find, sent} = editor();
  context.changeProfileScalingChoice('AddonXeSS');
  assert.equal(nodes.nativeScalingType.value, 'AddonXeSS');
  assert.equal(context.nativeSettingsPayload().ScalingType, 'NIS');
  assert.equal(context.addonEditorDraft.addons.XESSUPSC._enabled, true);
  assert.equal(find('Enable XeSS Upscaler'), undefined);
  assert.ok(find('Sharpening'));
  assert.equal(nodes.nativeScalingSharpnessRow.hidden, true);
  context.saveAddonEditor();
  assert.equal(sent.length, 0, 'scaling and enable flags save together');
  context.changeProfileScalingChoice('NIS');
  assert.equal(context.addonEditorDraft.addons.XESSUPSC._enabled, false);
  assert.equal(nodes.nativeScalingSharpnessRow.hidden, false);
});

test('missing manager or upscaler is explained without enabling the global add-on toggle', () => {
  const {context, nodes} = editor();
  context.controlSettings.addonManagerEnabled = false;
  context.changeProfileScalingChoice('AddonDLSS');
  assert.match(nodes.profileScalingTypeHint.textContent, /Add-ons/);
  assert.equal(context.controlSettings.addonManagerEnabled, false);
  context.controlSettings.addonManagerEnabled = true;
  context.addonEditorCatalog.find(addon => addon.id === 'DLSS4DLAA').installed = false;
  context.renderProfileScalingChoice();
  assert.match(nodes.profileScalingTypeHint.textContent, /not installed/);
});

test('cards disclose settings without enabling processing and preserve the user expansion', () => {
  const {context, nodes, find} = editor();
  const card = nodes.addonEditorCards.children[0];
  assert.equal(card.children[1].hidden, true);
  find('Configure DLSS 5').events.click();
  assert.equal(card.children[1].hidden, false);
  assert.equal(context.addonEditorDraft.addons.DLSSNR02._enabled, false);
  context.renderAddonEditor();
  assert.equal(nodes.addonEditorCards.children[0].children[1].hidden, false);
  assert.equal(nodes.addonEditorSummary.textContent, '0 enabled for this profile');
  const toggle = find('Enable DLSS 5');
  toggle.checked = true;
  toggle.events.change();
  assert.equal(nodes.addonEditorSummary.textContent, '1 enabled for this profile');
});

test('search changes presentation without saving or discarding unmatched settings', () => {
  const {context, nodes, sent, find} = editor();
  const before = JSON.stringify(context.addonEditorDraft);
  nodes.addonEditorSearch.value = 'resolution';
  context.renderAddonEditor();
  assert.equal(nodes.addonEditorCards.children.length, 1, 'search only shows basic external features');
  assert.ok(find('Resolution'));
  assert.equal(nodes.addonEditorCards.children[0].children[1].hidden, false);
  assert.equal(JSON.stringify(context.addonEditorDraft), before);
  assert.equal(sent.length, 0);
  nodes.addonEditorSearch.value = 'no matching add-on';
  context.renderAddonEditor();
  assert.match(nodes.addonEditorCards.children[0].textContent, /No add-ons match/);
  assert.equal(JSON.stringify(context.addonEditorDraft), before);
});

test('basic controls retain advanced and unknown values without exposing them', () => {
  const {context, find, sent} = editor();
  context.addonEditorCatalog[0].fields.push({key: 'flowQuality', label: 'Optical flow quality', type: 'number'});
  context.addonEditorDraft.addons.DLSSNR02.flowQuality = '3';
  context.addonEditorDraft.addons.DLSSNR02.futureValue = 7;
  context.renderAddonEditor();
  assert.equal(find('Optical flow quality'), undefined);
  assert.equal(find('future Value'), undefined);
  const resolution = find('Resolution');
  resolution.value = '75';
  resolution.events.change();
  context.saveAddonEditor();
  const saved = sent.at(-1).config.addons;
  assert.equal(saved.DLSSNR02.flowQuality, '3');
  assert.equal(saved.DLSSNR02.futureValue, 7);
  assert.equal(saved.Future.nested.flag, true);
});

test('sharpening follows the selected scaler and migrates legacy strength only on edit', () => {
  const {context, nodes, find} = editor();
  context.addonEditorDraft.addons.DLSS4DLAA = {_enabled: true, sharpen: '0.8'};
  context.renderAddonEditor();
  assert.equal(find('Sharpening').value, '0.5');
  assert.equal(context.addonEditorDraft.addons.DLSS4DLAA.sharpen, '0.8');
  const sharpness = find('Sharpening');
  sharpness.value = '0.7';
  sharpness.events.change();
  assert.equal(context.addonEditorDraft.addons.DLSS4DLAA.sharpen, '0.7');
  assert.equal(context.addonEditorDraft.addons.DLSS4DLAA.sharpenScale, '1.6');
  assert.equal(context.addonEditorDraft.addons.DLSS4DLAA.scalerPerGame, '0');
  context.changeProfileScalingChoice('AddonFSR');
  assert.equal(find('DLSS upscaling preset'), undefined);
  assert.equal(find('Sharpening').value, '');
  assert.equal(nodes.nativeScalingSharpnessRow.hidden, true);
});

test('included shader presets have edit controls and imported presets have named delete controls', () => {
  const list = {innerHTML: ''};
  const context = vm.createContext({
    document: {getElementById: () => list},
    escapeHtml: text => String(text).replaceAll('&', '&amp;').replaceAll('"', '&quot;'),
    reshadeProfiles: [{id: 'included', name: 'Built in', builtin: true, shaders: {pack: ['one.fx']}},
      {id: 'custom', name: 'My preset', imported_preset: true}],
  });
  vm.runInContext(html.slice(html.indexOf('    function renderReshadeProfileList()'),
    html.indexOf('    function updateShaderPackSelectionState(')), context);
  context.renderReshadeProfileList();
  assert.match(list.innerHTML, /Included preset/);
  assert.doesNotMatch(list.innerHTML, /deleteReshadeProfile\('included'\)/);
  assert.match(list.innerHTML, /aria-label="Edit Built in"/);
  assert.match(list.innerHTML, /aria-label="Delete My preset"/);
  assert.match(list.innerHTML, /Imported preset/);
});

test('an unavailable processor cannot be newly enabled and host-disabled profiles remain editable', () => {
  const {context, nodes, find} = editor();
  context.addonEditorCatalog[0].installed = false;
  context.controlSettings.addonManagerEnabled = false;
  context.renderAddonEditor();
  assert.equal(find('Enable DLSS 5').disabled, true);
  assert.equal(nodes.addonHostNotice.hidden, false);
  assert.ok(find('Resolution'));
  context.addonEditorDraft.addons.DLSSNR02._enabled = true;
  context.renderAddonEditor();
  assert.equal(find('Enable DLSS 5').disabled, false, 'a missing processor can still be turned off');
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

test('automatic AMD backend stays internal and runtime status uses its inference metrics', () => {
  const {context, nodes} = editor();
  context.addonEditorDraft.addons['LSC-AMDNR'] = {_enabled: true, dataPath: 'cache'};
  context.renderAddonEditor();
  const labels = [];
  const walk = node => { if (node['aria-label']) labels.push(node['aria-label']); node.children.forEach(walk); };
  walk(nodes.addonEditorCards);
  assert.equal(labels.some(label => label.includes('LSC-AMDNR')), false);
  nodes.addonRuntimeStatus = new Element('div');
  context.renderAddonRuntimeStatus({available: true, hostVersion: 'build-5-lsc.3', neuralBackend: 'AMD',
    addons: [{id: 'DLSSNR02', enabled: true, status: 'NVIDIA waiting'}, {id: 'LSC-AMDNR', enabled: true, status: 'AMD NR active'}],
    metrics: {'LSC-AMDNR': {results: 2, inference_fps: 24.5, result_age: 3, flow_ms: 1.25}}});
  assert.match(nodes.addonRuntimeStatus.textContent, /AMD NR active/);
  assert.match(nodes.addonRuntimeStatus.textContent, /24.5 NR evaluations\/s/);
  assert.doesNotMatch(nodes.addonRuntimeStatus.textContent, /NVIDIA waiting/);
  context.renderAddonRuntimeStatus({available: true, hostVersion: 'build-5-lsc.3', neuralBackend: 'AMD', addons: [], metrics: {}});
  assert.match(nodes.addonRuntimeStatus.textContent, /No inference result confirmed/);
});

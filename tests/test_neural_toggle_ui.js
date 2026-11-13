const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const start = html.indexOf('    function configureNeuralCapture()');
const end = html.indexOf('    function changeProfileScalingChoice', start);

test('NR configures WGC and saves capture and add-on changes together', () => {
  for (const [fg, scaling, expectedFg, expectedScaling] of [
    ['OFF', 'LS1', 'OFF', 'NIS'], ['OFF', 'Off', 'OFF', 'Off'],
    ['OFF', 'AddonDLSS', 'OFF', 'AddonDLSS'], ['OFF', 'AddonFSR', 'OFF', 'AddonFSR'],
    ['LSFG1', 'LS1', 'LSFG3', 'LS1'], ['LSFG2', 'NIS', 'LSFG3', 'NIS'],
    ['LSFG3', 'FSR', 'LSFG3', 'FSR']
  ]) {
    const capture = {value: 'DXGI'}, generation = {value: fg}, scaler = {value: scaling};
    let saves = 0, renders = 0;
    const context = {document: {
      querySelector() { return capture; },
      getElementById(id) { return id === 'nativeFrameGenerationType' ? generation : scaler; }
    }, serializedScalingType(value) { return value.startsWith('Addon') ? 'NIS' : value; },
    profileUpscalerChanged() { saves++; }, renderFrameGenerationModeSettings() { renders++; }};
    vm.runInNewContext(html.slice(start, end), context);
    context.configureNeuralCapture();
    assert.equal(capture.value, 'WGC');
    assert.equal(generation.value, expectedFg);
    assert.equal(scaler.value, expectedScaling);
    assert.equal(saves, 1);
    assert.equal(renders, 1);
  }
});

const runtimeStart = html.indexOf('    function renderAddonRuntimeStatus(status)');
const runtimeEnd = html.indexOf('    function renderAddonEditor()', runtimeStart);
function runtimeStatus(status) {
  const output = {setAttribute(_, value) { this.state = value; }};
  const context = {document: {getElementById() { return output; }}};
  vm.runInNewContext(html.slice(runtimeStart, runtimeEnd), context);
  context.renderAddonRuntimeStatus({available: true, hostVersion: 'test', ...status});
  return output;
}

test('NR runtime marks native capture errors and missing backends as errors', () => {
  let output = runtimeStatus({neuralBackend: 'NVIDIA', addons: [
    {id: 'DLSSNR02', enabled: true, loaded: true, level: 2, status: 'Select WGC'}]});
  assert.equal(output.state, 'error');
  assert.match(output.textContent, /Select WGC/);
  output = runtimeStatus({neuralBackend: 'NVIDIA', addons: [
    {id: 'DLSSNR02', enabled: true, loaded: false}]});
  assert.equal(output.state, 'error');
  assert.match(output.textContent, /did not load/);
});

test('NR runtime distinguishes waiting and enabled settings from completed GPU work', () => {
  const waiting = runtimeStatus({neuralBackend: 'waiting'});
  assert.match(waiting.textContent, /Waiting for capture GPU/);
  assert.doesNotMatch(waiting.textContent, /disabled/);
  const active = {neuralBackend: 'NVIDIA', addons: [
    {id: 'DLSSNR02', enabled: true, loaded: true, level: 0, status: 'Ready'}]};
  assert.match(runtimeStatus(active).textContent, /No completed NR GPU stages/);
  assert.match(runtimeStatus({...active, metrics: {DLSSNR02: {model_ms: 5.3, flow_ms: 2}}}).textContent,
    /Last completed NR GPU stages: 7.30 ms/);
});
const assertRuntimeCapture = (capture) => runtimeStatus({neuralBackend: 'NVIDIA',
  addons: [{id: 'DLSSNR02', enabled: true, loaded: true, status: 'Waiting for a real capture'}], capture}).textContent;

test('capture status distinguishes missing settings, idle hooks and unmatched rendering', () => {
  assert.match(assertRuntimeCapture({configured: false, dispatches: 0, captures: 0}), /No applied LS capture settings/);
  assert.match(assertRuntimeCapture({configured: true, dispatches: 0, captures: 0, captureApi: 1,
    frameGeneration: 1, scaler: 3, runtimeVersion: '3.2.2.0'}), /No LS compute work observed/);
  const unmatched = assertRuntimeCapture({configured: true, dispatches: 150, captures: 0,
    captureApi: 1, frameGeneration: 1, scaler: 3, runtimeVersion: '3.2.2.0'});
  assert.match(unmatched, /0 frames \/ 150 compute dispatches/);
  assert.match(unmatched, /no supported capture matched/);
  assert.match(unmatched, /Applied capture: WGC; FG LSFG 3; Scaling NIS; LS 3.2.2.0/);
  const ready = assertRuntimeCapture({configured: true, dispatches: 150, captures: 50,
    captureApi: 1, frameGeneration: 0, scaler: 3, runtimeVersion: '3.2.2.0'});
  assert.match(ready, /50 frames \/ 150 compute dispatches/);
  assert.doesNotMatch(ready, /no supported capture matched|No LS compute/);
});

test('runtime status reaches both global and profile views and clears stale success on disconnect', () => {
  const nodes = Object.fromEntries(['addonRuntimeStatus', 'profileAddonRuntimeStatus'].map(id => [id, {
    setAttribute(_, value) { this.state = value; }, textContent: ''}]));
  const context = {document: {getElementById(id) { return nodes[id]; }}};
  vm.runInNewContext(html.slice(runtimeStart, runtimeEnd), context);
  context.renderAddonRuntimeStatus({available: true, hostVersion: 'test', neuralBackend: 'NVIDIA',
    addons: [{id: 'DLSSNR02', enabled: true, loaded: true, status: 'Waiting'}]});
  assert.equal(nodes.profileAddonRuntimeStatus.textContent, nodes.addonRuntimeStatus.textContent);
  assert.match(nodes.profileAddonRuntimeStatus.textContent, /NVIDIA/);
  context.renderAddonRuntimeStatus({available: false, reason: 'Lossless Scaling stopped'});
  for (const node of Object.values(nodes)) {
    assert.equal(node.state, 'unavailable');
    assert.equal(node.textContent, 'Lossless Scaling stopped');
  }
});

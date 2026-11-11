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

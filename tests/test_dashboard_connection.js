const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const healthCode = html.slice(html.indexOf('    let wsHeartbeatTimer ='), html.indexOf('    let dashboardSessionToken ='));

test('host settings omit runtime details while keeping the enabled toggle and advanced panel action', () => {
  const host = html.slice(html.indexOf('<section class="addon-card addon-card-wide addon-host-card">'),
    html.indexOf('<section class="addon-package-section"'));
  assert.doesNotMatch(host, /addonRuntimeStatus|addon-host-runtime|Runtime status/);
  assert.match(host, /id="addonManagerEnabled"[^>]*checked/);
  assert.match(host, /id="openProxyMenuBtn"/);
  assert.doesNotMatch(html, /id="profileAddonRuntimeStatus"/);
});

test('display settings omit routing diagnostics and clean install has a decorative recycle icon', () => {
  const display = html.slice(html.indexOf('<h2 class="settings-group-title">Display &amp; scaling'),
    html.indexOf('<h2 class="settings-group-title">App &amp; updates'));
  assert.doesNotMatch(html, /gpuRoutingStatus|gpuIndexRefreshStatus/);
  assert.match(display, /id="preferredScalingGpu"/);
  assert.match(display, /id="gpuCalibrationStatus"/);
  assert.match(html, /id="cleanInstallBtn"[^>]*><span aria-hidden="true">&#9851;<\/span>/);
});

function dashboard(settingsReady = true) {
  let now = 0;
  let tick;
  let cleared = 0;
  const nodes = new Map();
  const status = [];
  const sent = [];
  const socket = {readyState: 1, send: value => sent.push(JSON.parse(value)), close() { this.closed = true; }};
  const context = vm.createContext({
    ws: socket, WebSocket: {OPEN: 1}, Date: {now: () => now}, generalSettingsReady: settingsReady,
    document: {getElementById(id) { if (!nodes.has(id)) nodes.set(id, {attributes: {}, setAttribute(key, value) { this.attributes[key] = value; }}); return nodes.get(id); }},
    setInterval(fn) { tick = fn; return 1; }, clearInterval() { cleared++; },
    setRuntimeStatus: (...args) => status.push(args),
  });
  vm.runInContext(healthCode, context);
  return {context, nodes, status, socket, sent, cleared: () => cleared,
          advance(ms) { now += ms; tick(); }};
}

test('disconnect makes controls inert and explicitly marks displayed state as stale', () => {
  const app = dashboard();
  app.context.setCompanionConnected(false);
  assert.equal(app.nodes.get('dashboardContent').inert, true);
  assert.match(app.nodes.get('companionConnectionBanner').textContent, /disconnected.*stale/i);
  assert.deepEqual(app.status.at(-1), ['Companion disconnected', 'inactive']);
  app.context.setCompanionConnected(true);
  assert.equal(app.nodes.get('dashboardContent').inert, false);
  assert.equal(app.nodes.get('companionConnectionBanner').hidden, true);
});

test('an initial connection cannot expose defaults before saved settings are rendered', () => {
  const app = dashboard(false);
  app.context.setCompanionConnected(true);
  assert.equal(app.nodes.get('dashboardContent').inert, true);
  assert.equal(app.nodes.get('dashboardContent').attributes['aria-busy'], 'true');
  assert.equal(app.nodes.get('dashboardLoading').hidden, false);
  app.context.generalSettingsReady = true;
  app.context.updateDashboardReadiness();
  assert.equal(app.nodes.get('dashboardContent').inert, false);
  assert.equal(app.nodes.get('dashboardContent').attributes['aria-busy'], 'false');
  assert.equal(app.nodes.get('dashboardLoading').hidden, true);
});

test('settings arriving before runtime state do not enable disconnected controls', () => {
  const app = dashboard(false);
  app.context.generalSettingsReady = true;
  app.context.updateDashboardReadiness();
  assert.equal(app.nodes.get('dashboardContent').inert, true);
  app.context.setCompanionConnected(true);
  assert.equal(app.nodes.get('dashboardContent').inert, false);
});

test('the loading screen is present before scripts and released only after settings render', () => {
  assert.match(html, /id="dashboardContent" inert aria-busy="true"/);
  assert.match(html, /#dashboardContent\[aria-busy="true"\] \{ visibility: hidden; \}/);
  const settingsHandler = html.slice(html.indexOf("} else if (msg.type === 'GENERAL_SETTINGS'"), html.indexOf("} else if (msg.type === 'SCALE_NOW_SCHEDULED'"));
  assert.ok(settingsHandler.indexOf('renderControlSettings();') < settingsHandler.indexOf('generalSettingsReady = true;'));
  assert.ok(settingsHandler.indexOf('generalSettingsReady = true;') < settingsHandler.indexOf('updateDashboardReadiness();'));
});

test('saved settings are requested before slower graphics and addon probes', () => {
  const sent = [];
  const socket = {send: value => sent.push(JSON.parse(value).type)};
  const context = vm.createContext({socket, ws: socket, lastStateRevision: 0,
    startHeartbeat() {}, publishEditorActivity() {}, addonEditorUnsaved: false});
  const onOpen = html.slice(html.indexOf('      ws.onopen = () => {'), html.indexOf('      ws.onmessage = (event) => {'));
  vm.runInContext(onOpen, context);
  socket.onopen();
  assert.equal(sent[0], 'GET_GENERAL_SETTINGS');
  assert.equal(sent.filter(type => type === 'GET_GENERAL_SETTINGS').length, 1);
  assert.ok(sent.includes('GET_GRAPHICS_STATUS'));
  assert.ok(sent.includes('GET_ADDON_RUNTIME_STATUS'));
});

test('a stalled backend disconnects after missed heartbeats', () => {
  const app = dashboard();
  app.context.startHeartbeat(app.socket);
  for (let i = 0; i < 7; i++) app.advance(1000);
  assert.equal(app.socket.closed, true);
  assert.equal(app.nodes.get('dashboardContent').inert, true);
  assert.match(app.nodes.get('companionConnectionBanner').textContent, /not responding/);
  assert.equal(app.sent[0].healthcheck, true);
});

test('resume from sleep allows a fresh health check before declaring a hang', () => {
  const app = dashboard();
  app.context.startHeartbeat(app.socket);
  app.advance(60000);
  assert.equal(app.socket.closed, undefined);
  assert.equal(app.sent.length, 1);
});

test('replaced sockets cannot send heartbeats and timers are cleaned up', () => {
  const app = dashboard();
  app.context.startHeartbeat(app.socket);
  app.context.ws = {readyState: 1};
  app.advance(1000);
  assert.equal(app.sent.length, 0);
  app.context.stopHeartbeat();
  assert.equal(app.cleared(), 1);
});

test('pending scaling commands are visible without claiming scaling is active', () => {
  const status = [];
  const context = vm.createContext({runtimeScalingActive: false, runtimeActiveProfile: null,
    renderProxyMenuAction() {}, renderRuntimeScalingStatus() {}, setRuntimeStatus: (...args) => status.push(args)});
  const runtimeCode = html.slice(html.indexOf('    function renderRuntime(msg)'), html.indexOf('    function activeProfileUsesProxy()'));
  vm.runInContext(runtimeCode, context);
  context.renderRuntime({isScalingActive: false, scalingControl: {state: 'pending', message: 'Waiting for scaling confirmation'}});
  assert.deepEqual(status.at(-1), ['Waiting for scaling confirmation', 'inactive']);
  assert.equal(context.runtimeScalingActive, false);
});

test('runtime addon failures replace the green scaling label while stop stays available', () => {
  const status = [];
  const context = vm.createContext({runtimeScalingActive: false, runtimeActiveProfile: null,
    renderProxyMenuAction() {}, renderRuntimeScalingStatus() {}, setRuntimeStatus: (...args) => status.push(args)});
  vm.runInContext(html.slice(html.indexOf('    function renderRuntime(msg)'), html.indexOf('    function activeProfileUsesProxy()')), context);
  const running = {isScalingActive: true, losslessScalingFound: true, losslessScalingRunning: true};
  context.renderRuntime({...running, scalingRuntimeError: 'NR unavailable: runtime missing'});
  assert.deepEqual(status.at(-1), ['NR unavailable: runtime missing', 'not-found']);
  assert.equal(context.runtimeScalingActive, true);
  context.renderRuntime({...running, scalingRuntimeError: ''});
  assert.equal(status.at(-1)[1], 'active');
});

function benchmarkDashboard() {
  const nodes = new Map();
  const element = () => ({
    children: [],
    replaceChildren() { this.children = []; },
    appendChild(child) { this.children.push(child); },
    set innerHTML(_value) { throw new Error('Benchmark results must render as text'); },
  });
  const context = vm.createContext({document: {
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); },
    createElement: element,
  }});
  const code = html.slice(html.indexOf('    function renderDynamicLimiterBenchmark(result)'), html.indexOf('    function renderBenchmarkStatus(msg)'));
  vm.runInContext(code, context);
  return {nodes, render: result => context.renderDynamicLimiterBenchmark(result)};
}

test('adaptive benchmark renders independent verdicts and timestamped LS timing safely', () => {
  const app = benchmarkDashboard();
  const message = '<img src=x onerror=alert(1)>';
  app.render({dynamicLimiter: {
    status: 'inconclusive', policy_version: 2,
    telemetry_validity: {status: 'pass'}, cap_enforcement: {status: 'pass'},
    ls_benefit: {status: 'inconclusive'}, monitoring_overhead: {status: 'pass'},
  }, limiterTrace: [{elapsed_seconds: 31, current_limit: 91, rtss_readback_limit: 91,
    pacing: {p95Ms: 12.345}, lossless_gpu_percent: 5, gpu_headroom_percent: 26,
    state: 'no_response', message}]});
  const summary = app.nodes.get('dynamicLimiterBenchmarkSummary').textContent;
  assert.match(summary, /Telemetry: pass/);
  assert.match(summary, /Source cap: pass/);
  assert.match(summary, /LS benefit: inconclusive/);
  assert.match(summary, /Monitoring cost: pass/);
  assert.match(summary, /Absolute display delivery unverified/);
  assert.doesNotMatch(summary, /Reserve sustained/);
  assert.equal(app.nodes.get('dynamicLimiterBenchmarkResults').hidden, false);
  const rows = app.nodes.get('dynamicLimiterBenchmarkTrace').children;
  assert.equal(rows.length, 1);
  assert.deepEqual(rows[0].children.map(cell => cell.textContent),
    ['31.0', 91, 91, '12.35', 5, 26, `no_response: ${message}`]);
});

test('legacy or unknown benchmark policies show a migration notice and clear old traces', () => {
  const app = benchmarkDashboard();
  for (const policy of [undefined, 0, 1, 3]) {
    app.render({dynamicLimiter: {status: 'running', policy_version: 2},
      limiterTrace: [{elapsed_seconds: 1, state: 'observing'}]});
    assert.equal(app.nodes.get('dynamicLimiterBenchmarkTrace').children.length, 1);
    app.render({dynamicLimiter: {status: 'pass', policy_version: policy, target_percent: 25}});
    assert.match(app.nodes.get('dynamicLimiterBenchmarkSummary').textContent, /Previous policy report.*adaptive acceptance benchmark/);
    assert.equal(app.nodes.get('dynamicLimiterBenchmarkTrace').children.length, 0);
  }
  app.render(null);
  assert.equal(app.nodes.get('dynamicLimiterBenchmarkResults').hidden, true);
  assert.equal(app.nodes.get('dynamicLimiterBenchmarkTrace').children.length, 0);
});

test('missing adaptive verdicts stay unverified rather than inheriting an overall pass', () => {
  const app = benchmarkDashboard();
  app.render({dynamicLimiter: {status: 'pass', policy_version: 2, telemetry_validity: {status: 'pass'}}});
  const summary = app.nodes.get('dynamicLimiterBenchmarkSummary').textContent;
  assert.match(summary, /Telemetry: pass/);
  assert.match(summary, /Source cap: unverified/);
  assert.match(summary, /LS benefit: unverified/);
  assert.match(summary, /Monitoring cost: unverified/);
  app.render({dynamicLimiter: {status: 'running', policy_version: 2, message: 'Monitoring off: phase 1/4'}});
  assert.equal(app.nodes.get('dynamicLimiterBenchmarkSummary').textContent, 'Monitoring off: phase 1/4');
});

test('live limiter progress is routed to the renderer and accumulates adjustment samples', () => {
  const rendered = [];
  const context = vm.createContext({renderDynamicLimiterBenchmark: result => rendered.push(result)});
  const code = html.slice(html.indexOf('    let dynamicBenchmarkTrace = [];'), html.indexOf('    function renderDynamicLimiterBenchmark(result)'));
  vm.runInContext(code, context);
  context.receiveDynamicLimiterProgress({sample: {current_limit: 120, lossless_gpu_percent: 10}, targetPercent: 25});
  context.receiveDynamicLimiterProgress({sample: {current_limit: 119, lossless_gpu_percent: 12}, targetPercent: 25});
  assert.equal(rendered.at(-1).dynamicLimiter.status, 'running');
  assert.equal(rendered.at(-1).dynamicLimiter.policy_version, 2);
  assert.equal(rendered.at(-1).dynamicLimiter.adjustment_count, 1);
  assert.equal(rendered.at(-1).limiterTrace.length, 2);
  assert.match(html, /msg.type === 'DYNAMIC_LIMITER_PROGRESS'\) \{\s*receiveDynamicLimiterProgress\(msg\);/);
  const started = html.slice(html.indexOf("} else if (msg.type === 'BENCHMARK_STARTED')"), html.indexOf("} else if (msg.type === 'GPU_CALIBRATION_STATUS')"));
  assert.match(started, /getElementById\('startDynamicLimiterBenchmarkBtn'\)\.disabled = true/);
});

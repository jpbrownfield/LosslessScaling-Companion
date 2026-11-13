const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const providers = ['ls-addon-manager', 'sm86-sr', 'amd-neural-render-runtime', 'display-commander', 'reshade', 'special-k'];
const versions = {'ls-addon-manager': 'build-7-lsc.4', 'sm86-sr': '0.4.1', 'amd-neural-render-runtime': '1.3.2',
  'display-commander': '2.0.0', reshade: '6.8.0', 'special-k': 'SK_26_6_13_7'};
class Element {
  constructor() { this.dataset = {}; this.textContent = 'Download'; this.children = []; this.attributes = {};
    this.classes = new Set(); this.classList = {toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name)}; }
  setAttribute(key, value) { this.attributes[key] = value; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
}
function dashboard() {
  const nodes = new Map(), buttons = new Map(), links = new Map();
  const node = selector => { if (!nodes.has(selector)) nodes.set(selector, new Element()); return nodes.get(selector); };
  providers.forEach(provider => {
    const element = new Element();
    if (['reshade', 'special-k'].includes(provider)) { element.dataset.addonDownload = provider; links.set(provider, element); }
    else { element.dataset.addonInstall = provider; buttons.set(provider, element); }
  });
  const context = vm.createContext({
    graphicsPackages: [], graphicsExternalSources: {}, dlssnrRuntimes: [], latestAddonVersions: {},
    addonUpdateItems: [], componentOperation: null, companionUpdateBusy: false,
    renderExternalSourceStatus() {}, renderNotificationBell() {}, updateNotificationsEnabled: () => true,
    componentProgressLabel: () => 'Downloading...', setGraphicsStatus() {},
    document: {querySelector: node, getElementById: node, createElement: () => new Element(),
      querySelectorAll: selector => [...(selector === '[data-addon-install]' ? buttons : links).values()]},
    localStorage: {getItem: () => null},
  });
  vm.runInContext(html.slice(html.indexOf('    function normalizedAddonVersion('), html.indexOf('    function renderExternalSourceStatus(')), context);
  vm.runInContext(html.slice(html.indexOf('    function renderGraphicsStatus('), html.indexOf('    function renderReshadeMonitorNits(')), context);
  vm.runInContext(html.slice(html.indexOf('    function renderAddonUpdateNotice('), html.indexOf('    function componentProgressLabel(')), context);
  const refresh = packages => context.renderGraphicsStatus({packages, externalSources: context.graphicsExternalSources});
  return {context, nodes, buttons, links, refresh};
}
const installed = () => Object.entries(versions).map(([provider, version]) => ({provider, version}));

test('automatic metadata disables all current downloads regardless of message arrival order', () => {
  for (const metadataFirst of [true, false]) {
    const {context, buttons, links, refresh} = dashboard();
    if (metadataFirst) context.renderAddonUpdateNotice([], versions);
    refresh(installed());
    if (!metadataFirst) context.renderAddonUpdateNotice([], versions);
    for (const button of buttons.values()) { assert.equal(button.disabled, true); assert.equal(button.textContent, 'Up to date'); }
    for (const link of links.values()) { assert.equal(link.attributes['aria-disabled'], 'true'); assert.equal(link.tabIndex, -1); }
    refresh(installed());
    assert.equal(buttons.get('ls-addon-manager').disabled, true, 'a status refresh preserves the disabled state');
  }
});

test('current versions stay disabled across operation completion and failure; newer releases unlock them', () => {
  const {context, buttons, refresh} = dashboard();
  refresh(installed());
  context.renderAddonUpdateNotice([], versions);
  for (const phase of ['checking', 'complete', 'failed']) {
    context.componentOperation = {provider: 'reshade', busy: phase === 'checking', phase};
    context.updateAddonInstallButtons();
    assert.equal(buttons.get('sm86-sr').disabled, true);
  }
  context.componentOperation = null;
  context.renderGraphicsReleases({provider: 'sm86-sr', releases: [{version: '0.4.2'}]});
  assert.equal(buttons.get('sm86-sr').disabled, false);
  assert.equal(buttons.get('sm86-sr').textContent, 'Download');
});

test('version checks keep installed and available versions visible and use metadata for release links', () => {
  const {context, nodes, refresh, buttons} = dashboard();
  refresh([{provider: 'ls-addon-manager', version: 'build-9-lsc.2'}]);
  const url = 'https://github.com/FriedSnowman/ls-addon-manager/releases/tag/build-9';
  context.renderGraphicsReleases({provider: 'ls-addon-manager', releases: [{version: 'build-9-lsc.2', html_url: url}]});
  const text = nodes.get('[data-addon-status="ls-addon-manager"]').textContent;
  assert.match(text, /Installed version: build-9-lsc.2/);
  assert.match(text, /Bundled version: build-9-lsc.2/);
  assert.match(text, /Up to date/);
  assert.equal(nodes.get('addonHostReleaseLink').href, url);
  assert.equal(buttons.get('ls-addon-manager').disabled, true);
});

test('detected installations and equivalent versions disable links before import finishes', () => {
  const {context, links, refresh} = dashboard();
  context.graphicsExternalSources = {reshade: {detected: true, version: '6.8'}, 'special-k': {detected: true, version: '26.6.13.7'}};
  refresh([]);
  context.renderAddonUpdateNotice([], versions);
  assert.equal(links.get('reshade').attributes['aria-disabled'], 'true');
  assert.equal(links.get('special-k').attributes['aria-disabled'], 'true');
  assert.equal(links.get('reshade').href, 'https://reshade.me/downloads/ReShade_Setup_6.8.0_Addon.exe');
});

test('version comparison preserves build families and prerelease identities and prevents downgrades', () => {
  const {context} = dashboard();
  for (const [current, latest, expected] of [
    ['build-7-lsc.4', 'build-7-lsc.3', true], ['build-7-lsc.3', 'build-7-lsc.4', false],
    ['7.4', 'build-7-lsc.4', false], ['6.8.0-beta', '6.8.0', false], ['6.8.1', '6.8.0', true],
    ['v6.8.0.0', '6.8', true], ['1.9.0', '1.10.0', false], ['', '', false],
  ]) assert.equal(context.addonVersionIsCurrent(current, latest), expected, `${current} vs ${latest}`);
});

test('missing and outdated packages stay downloadable and a failed check does not claim up to date', () => {
  const {context, buttons, refresh} = dashboard();
  refresh([{provider: 'sm86-sr', version: '0.4.0'}]);
  context.renderAddonUpdateNotice([], versions);
  assert.equal(buttons.get('sm86-sr').disabled, false);
  assert.equal(buttons.get('ls-addon-manager').disabled, false);
  context.renderGraphicsReleases({provider: 'sm86-sr', releases: []});
  assert.equal(buttons.get('sm86-sr').disabled, false);
});

test('installed-version display selects the newest version rather than filesystem order', () => {
  const {context, nodes, refresh} = dashboard();
  refresh([{provider: 'reshade', version: '6.10.0'}, {provider: 'reshade', version: '6.9.0'}]);
  context.renderAddonUpdateNotice([], {reshade: '6.10.0'});
  assert.match(nodes.get('[data-addon-status="reshade"]').textContent, /Installed version: 6.10.0/);
});

test('a stale notification cannot offer a download when its selected version is already current', () => {
  const {context, nodes, refresh} = dashboard();
  refresh([{provider: 'reshade', version: '6.8.0'}]);
  context.renderAddonUpdateNotice([{provider: 'reshade', name: 'ReShade', currentVersion: '6.8.0', latestVersion: '6.8.0'}], {reshade: '6.8.0'});
  const download = nodes.get('addonUpdateNotice').children[0].children[1].children[1];
  assert.equal(download.disabled, true);
  assert.equal(download.textContent, 'Downloaded');
  // A profile still pinned to an older version must retain its update action,
  // even if the archive for that update has already been staged.
  context.renderAddonUpdateNotice([{provider: 'reshade', name: 'ReShade', currentVersion: '6.7.0', latestVersion: '6.8.0'}], {reshade: '6.8.0'});
  assert.equal(nodes.get('addonUpdateNotice').children[0].children[1].children[1].disabled, false);
});

test('a cached NeuralRender package cannot disable repair of an incomplete bundle', () => {
  const {context} = dashboard();
  context.latestAddonVersions = {'lsp-neural-render': '1.0.0', 'lossless-proxy': '2.0.0'};
  context.graphicsPackages = [{provider: 'lsp-neural-render', version: '1.0.0'}];
  assert.equal(context.addonDownloadIsCurrent('lsp-neural-render'), false);
  context.graphicsPackages.push({provider: 'lossless-proxy', version: '2.0.0'});
  assert.equal(context.addonDownloadIsCurrent('lsp-neural-render'), false);
  context.dlssnrRuntimes = [{sha256: 'verified'}];
  assert.equal(context.addonDownloadIsCurrent('lsp-neural-render'), true);
});

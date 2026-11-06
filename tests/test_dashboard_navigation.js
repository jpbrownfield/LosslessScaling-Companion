const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const navigationCode = html.slice(html.indexOf('    let sectionNavTrigger = null;'), html.indexOf("    document.getElementById('checkCompanionUpdateBtn').onclick"));
const initializationCode = html.slice(html.indexOf("    const savedActivePanelId = localStorage.getItem('dashboard-active-panel');"), html.indexOf("    window.addEventListener('beforeunload'"));

function dashboard(saved = {}, query = '') {
  const nodes = new Map();
  const events = {};
  let document;
  const element = () => {
    const classes = new Set();
    return {
    hidden: false, attributes: {}, events: {}, textContent: '', children: [], dataset: {}, style: {}, offsetHeight: 260,
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(key, action) { this.events[key] = action; },
    focus(options) { this.focused = true; this.focusOptions = options; document.activeElement = this; },
    querySelector(selector) { return selector === '.nav-label' ? {textContent: this.label} : this.children[0] || null; },
    querySelectorAll() { return this.headings || this.children; },
    closest(selector) { return selector === '[hidden]' && this.hidden ? this : null; },
    replaceChildren() { this.children = []; },
    appendChild(child) { this.children.push(child); },
    getBoundingClientRect() { return {top: 120}; },
    scrollIntoView(options) { this.scrolled = options; },
    classList: {add: value => classes.add(value), remove: value => classes.delete(value),
      contains: value => classes.has(value), toggle(value, enabled) { if (enabled) classes.add(value); else classes.delete(value); }},
  }; };
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  const titles = [[], ['Startup', 'GPU Selection'], ['Echo LS Add-on Manager', 'Process Lasso integration', 'RTSS frame limiting', 'ReShade', 'ReShade profiles', 'Special K'], ['GPU workload', 'PresentMon'], []];
  const panels = ['profiles', 'general', 'lossless-addons', 'performance-benchmark', 'testing'].map((key, index) => {
    const panel = element();
    panel.id = key;
    panel.dataset.collapsibleId = key;
    panel.headings = titles[index].map(textContent => Object.assign(element(), {textContent, tagName: 'H2'}));
    panel.children = [Object.assign(element(), {textContent: key, tagName: 'H1'})];
    return panel;
  });
  const buttons = panels.map(panel => {
    const button = get(panel.dataset.collapsibleId);
    button.dataset = {dashboardPanel: panel.dataset.collapsibleId};
    button.label = panel.dataset.collapsibleId;
    get(`dashboard-subsections-${panel.dataset.collapsibleId}`).children = [element()];
    return button;
  });
  const storage = new Map(Object.entries(saved));
  const scrolls = [];
  document = {
    getElementById: get, createElement: element, querySelector: get,
    addEventListener(key, handler) { (events[key] ||= []).push(handler); },
  };
  const context = vm.createContext({
    majorDashboardPanels: panels, settingsDashboardPanels: panels.slice(1),
    profilesPanel: panels[0], generalSettingsPanel: panels[1], dashboardNavButtons: buttons,
    document,
    localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)},
    window: {scrollTo: value => scrolls.push(value), matchMedia: () => ({matches: true}), location: {search: query}, innerHeight: 900, addEventListener() {}},
    requestAnimationFrame: action => action(), renderProfiles() {}, URLSearchParams,
  });
  vm.runInContext(navigationCode, context);
  vm.runInContext(initializationCode, context);
  return {panels, buttons, context, get, storage, scrolls, events};
}

test('every sidebar action shows exactly one section and updates its accessible current state', () => {
  const {panels, buttons, get, storage, scrolls} = dashboard();
  buttons.forEach((button, index) => {
    get('notificationPopover').hidden = false;
    button.onclick();
    assert.deepEqual(panels.filter(panel => !panel.hidden), [panels[index]]);
    assert.equal(button.attributes['aria-current'], 'page');
    assert.equal(button.attributes['aria-pressed'], 'true');
    assert.equal(buttons.filter(item => item.attributes['aria-current']).length, 1);
    assert.equal(get('settingsArea').hidden, index === 0);
    assert.equal(storage.get('dashboard-active-panel'), panels[index].dataset.collapsibleId);
    assert.equal(get('notificationPopover').hidden, true);
    assert.equal(get('notificationBell').attributes['aria-expanded'], 'false');
  });
  assert.equal(scrolls.at(-1).behavior, 'instant');
});

test('saved and stale accordion states always open a usable section', () => {
  for (const saved of [{}, {'dashboard-active-panel': 'unknown'}, {'dashboard-active-panel': 'general', 'dashboard-panel-general': 'closed'}]) {
    const {panels} = dashboard(saved);
    assert.equal(panels.filter(panel => !panel.hidden).length, 1);
    assert.equal(panels.find(panel => !panel.hidden).dataset.collapsibleId, saved['dashboard-active-panel'] === 'general' ? 'general' : 'profiles');
  }
});

test('tray settings shortcut honors the last settings view and falls back from a stale view', () => {
  for (const saved of ['lossless-addons', 'deleted-view']) {
    const {panels} = dashboard({'dashboard-active-panel': 'profiles', 'dashboard-last-settings-panel': saved}, '?view=settings');
    assert.equal(panels.find(panel => !panel.hidden).dataset.collapsibleId, saved === 'lossless-addons' ? saved : 'general');
  }
});

test('arrow keys, Home, and End navigate the sidebar without changing the selected section', () => {
  const {buttons, panels} = dashboard();
  for (const [index, key, target] of [[0, 'ArrowUp', 4], [4, 'ArrowDown', 0], [2, 'Home', 0], [0, 'End', 4]]) {
    let prevented = false;
    buttons.forEach(button => { button.focused = false; });
    buttons[index].events.keydown({key, preventDefault() { prevented = true; }});
    assert.equal(buttons[target].focused, true);
    assert.equal(prevented, true);
    assert.equal(panels[0].hidden, false);
  }
});

test('clicking Add-ons lists its headings and selecting one focuses and scrolls to the visible section', () => {
  const {buttons, panels, get} = dashboard();
  buttons[2].onclick();
  const menu = get('dashboard-subsections-lossless-addons');
  const links = menu.children[0].children;
  assert.deepEqual(links.map(link => link.textContent), ['Echo LS Add-on Manager', 'Process Lasso integration', 'RTSS frame limiting', 'ReShade', 'ReShade profiles', 'Special K']);
  assert.equal(menu.classList.contains('is-open'), true);
  assert.equal(menu.inert, false);
  assert.equal(buttons[2].attributes['aria-expanded'], 'true');
  links[1].onclick();
  assert.equal(panels[2].headings[1].focused, true);
  assert.equal(panels[2].headings[1].scrolled.block, 'start');
  assert.equal(panels[2].headings[1].scrolled.behavior, 'instant');
  assert.equal(panels[2].hidden, false);
  assert.equal(menu.classList.contains('is-open'), true);
  assert.equal(buttons[2].attributes['aria-expanded'], 'true');
  assert.equal(links[1].attributes['aria-current'], 'location');
});

test('repeated click collapses nested links and Escape restores focus to their parent', () => {
  const {buttons, get} = dashboard();
  const menu = get('dashboard-subsections-general');
  buttons[1].onclick(); buttons[1].onclick();
  assert.equal(menu.classList.contains('is-open'), false);
  assert.equal(menu.inert, true);
  buttons[1].onclick();
  menu.children[0].events.keydown({key: 'Escape', preventDefault() {}});
  assert.equal(menu.classList.contains('is-open'), false);
  assert.equal(menu.attributes['aria-hidden'], 'true');
  assert.equal(buttons[1].focused, true);
});

test('right arrow opens subsection navigation and hidden headings are omitted', () => {
  const {buttons, panels, get} = dashboard();
  panels[1].headings[1].hidden = true;
  buttons[1].events.keydown({key: 'ArrowRight', preventDefault() {}});
  const links = get('dashboard-subsections-general').children[0].children;
  assert.equal(links.length, 1);
  assert.equal(links[0].focused, true);
});

test('switching sections collapses the previous nested navigation', () => {
  const {buttons, get} = dashboard();
  buttons[1].onclick(); buttons[2].onclick();
  assert.equal(get('dashboard-subsections-general').classList.contains('is-open'), false);
  assert.equal(get('dashboard-subsections-general').inert, true);
  assert.equal(get('dashboard-subsections-lossless-addons').classList.contains('is-open'), true);
});

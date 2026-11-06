const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    function profileIssueControls('), html.indexOf("    document.getElementById('notificationBell').onclick"));

class Element {
  constructor() {
    this.children = []; this.dataset = {}; this.attributes = {}; this.value = ''; this.title = '';
    this.classes = new Set();
    this.classList = {add: value => this.classes.add(value), remove: value => this.classes.delete(value)};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren() { this.children = []; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  querySelectorAll() { return []; }
  focus() { this.focused = true; }
}

function dashboard() {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const context = vm.createContext({
    controlSettings: {},
    notificationState: {settingsIssues: []}, operationIssues: new Map(),
    addonUpdateItems: [], companionUpdateState: null,
    document: {getElementById: get, createElement: () => new Element(),
      querySelectorAll(selector) { return selector === '[data-validation-highlight]'
        ? [...nodes.values()].filter(node => node.dataset.validationHighlight) : []; }},
    editProfile() {}, openMajorDashboardPanel() {},
  });
  vm.runInContext(code, context);
  return {context, get};
}

test('bell keeps red issue counts separate from green update counts', () => {
  const {context, get} = dashboard();
  context.addonUpdateItems = [{provider: 'reshade', currentVersion: '1', latestVersion: '2'}];
  context.companionUpdateState = {updateAvailable: true, latestVersion: '2'};
  context.receiveNotifications({settingsIssues: [{path: ['port'], field: 'port', value: 'bad', message: 'Enter a number'}], deploymentRecovery: {message: 'Recovery needed'}});
  assert.equal(get('notificationIssueCount').textContent, '2');
  assert.equal(get('notificationUpdateCount').textContent, '2');
  assert.equal(get('notificationIssueCount').hidden, false);
  context.receiveNotifications({settingsIssues: [], deploymentRecovery: null});
  assert.equal(get('notificationIssueCount').hidden, true);
  assert.equal(get('notificationUpdateCount').hidden, false);
});

test('profile editor highlights the exact invalid control and removes highlights on correction', () => {
  const {context, get} = dashboard();
  get('profileId').value = 'game';
  context.receiveNotifications({settingsIssues: [{profileId: 'game', path: ['profiles', 2, 'rtss', 'framerate_limit'], field: 'rtss.framerate_limit', value: 5000, message: 'Must be at most 1000'}]});
  assert.equal(get('rtssFramerateLimit').attributes['aria-invalid'], 'true');
  assert.equal(get('rtssFramerateLimit').classes.has('validation-invalid'), true);
  assert.equal(get('rtssMaximumFramerate').classes.has('validation-invalid'), false);
  assert.equal(get('profileRepairPanel').hidden, false);
  context.receiveNotifications({settingsIssues: []});
  assert.equal(get('rtssFramerateLimit').attributes['aria-invalid'], undefined);
  assert.equal(get('profileRepairPanel').hidden, true);
});

test('errors from another profile do not highlight the open profile', () => {
  const {context, get} = dashboard();
  get('profileId').value = 'other';
  context.receiveNotifications({settingsIssues: [{profileId: 'game', path: ['rtss', 'framerate_limit'], message: '<img src=x onerror=bad()>'}]});
  assert.equal(get('profileRepairPanel').hidden, true);
  assert.equal(get('rtssFramerateLimit').attributes['aria-invalid'], undefined);
  assert.equal(get('notificationList').children[0].children[0].textContent.includes('<img'), true);
});

test('numeric parsing errors open a numeric correction without JSON quoting', () => {
  const {context, get} = dashboard();
  context.openSettingRepair({field: 'port', path: ['port'], kind: 'int_parsing', value: 'bad', message: 'Enter a number'});
  assert.equal(get('settingRepairType').value, 'number');
  assert.equal(get('settingRepairValue').value, 'bad');
  assert.equal(get('settingRepairValue').focused, true);
});

test('companion update notification focuses the action in its visible settings group', () => {
  const {context, get} = dashboard();
  context.companionUpdateState = {updateAvailable: true, latestVersion: '2'};
  context.renderNotificationBell();
  get('notificationList').children[0].children[1].onclick();
  assert.equal(get('checkCompanionUpdateBtn').focused, true);
});

test('notification opt-out and automatic installation suppress updates while retaining errors', () => {
  for (const preferences of [{notifyOnUpdates: false}, {notifyOnUpdates: true, automaticallyInstallUpdates: true}]) {
    const {context, get} = dashboard();
    context.controlSettings = preferences;
    context.companionUpdateState = {updateAvailable: true, latestVersion: '2'};
    context.addonUpdateItems = [{provider: 'reshade', currentVersion: '1', latestVersion: '2'}];
    context.receiveNotifications({settingsIssues: [{field: 'port', message: 'Invalid value'}]});
    assert.equal(get('notificationUpdateCount').hidden, true);
    assert.equal(get('notificationIssueCount').hidden, false);
    assert.equal(get('notificationList').children.length, 1);
  }
});

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    let requestedProfileEditorId ='), html.indexOf('    window.editProfile ='));

function dashboard(search = '', active = false, currentId = '') {
  const opened = [], notices = [], sections = [], events = {};
  const context = vm.createContext({URLSearchParams,
    window: {location: {search}, addEventListener: (key, fn) => events[key] = fn,
      editProfile: id => opened.push(id)},
    profiles: [{id: 'game'}, {id: 'other'}], ws: {readyState: 1}, WebSocket: {OPEN: 1},
    document: {getElementById: id => id === 'profileId' ? {value: currentId} : {classList: {contains: () => active}}},
    openMajorDashboardPanel() {}, profilesPanel: {}, noticeDialog: text => notices.push(text),
    navigateProfileSection: section => sections.push(section),
  });
  vm.runInContext(code, context);
  return {context, opened, notices, sections, events};
}

test('cold profile link opens the saved profile once after summaries arrive', () => {
  const state = dashboard('?editProfile=game');
  vm.runInContext('openRequestedProfileEditor(); openRequestedProfileEditor()', state.context);
  assert.deepEqual(state.opened, ['game']);
});

test('reused dashboard opens addon settings without reloading or losing an existing draft', () => {
  const state = dashboard('', true, 'game');
  state.events['companion-edit-profile']({detail: {profileId: 'game', section: 'addons'}});
  assert.deepEqual(state.sections, ['profileAddonsPanel']);
  assert.deepEqual(state.opened, []);
  const other = dashboard('', true, 'other');
  other.events['companion-edit-profile']({detail: {profileId: 'game'}});
  assert.deepEqual(other.opened, []);
  assert.equal(other.notices.length, 1);
});

test('unknown profile ids and malformed navigation events cannot open an editor', () => {
  const state = dashboard('?editProfile=missing');
  vm.runInContext('openRequestedProfileEditor()', state.context);
  state.events['companion-edit-profile']({detail: {profileId: 12}});
  assert.deepEqual(state.opened, []);
});

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    function profileAvatar('), html.indexOf('    function renderProfiles('));
const context = vm.createContext({dashboardSessionToken: 'session-token',
  escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;')});
vm.runInContext(code, context);

test('existing profiles request their application icons without adding saved icon fields', () => {
  const profile = {id: 'existing-profile', name: 'Game'};
  const before = JSON.stringify(profile);
  const markup = context.profileAvatar(profile);
  assert.match(markup, /src="\/profile-icon\/existing-profile.png\?token=session-token&amp;v="/);
  assert.match(markup, /loading="lazy"/);
  assert.doesNotMatch(markup, />G</);
  assert.equal(JSON.stringify(profile), before);
});

test('retargeted profiles request the new version while web profiles use a local globe', () => {
  const a = context.profileAvatar({id: 'game', iconVersion: 'old'});
  const b = context.profileAvatar({id: 'game', iconVersion: 'new'});
  assert.notEqual(a, b);
  const web = context.profileAvatar({id: 'website', target_domain: 'example.test'});
  assert.doesNotMatch(web, /<img|https?:/);
  assert.match(web, /<circle/);
});

test('loaded icons hide the SVG fallback and failed icons restore it', () => {
  const markup = context.profileAvatar({id: 'game'});
  const attributes = {};
  const image = {previousElementSibling: {
    setAttribute: (key, value) => { attributes[key] = value; },
    removeAttribute: key => { delete attributes[key]; },
  }};
  const load = /onload="([^"]+)"/.exec(markup)[1];
  const error = /onerror="([^"]+)"/.exec(markup)[1];
  new Function(load).call(image);
  assert.equal(attributes.hidden, '');
  new Function(error).call(image);
  assert.equal(image.hidden, true);
  assert.equal(attributes.hidden, undefined);
});

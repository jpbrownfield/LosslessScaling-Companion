const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('companion/ui/dashboard.html', 'utf8');
const code = html.slice(html.indexOf('    function setActiveProfileSection('), html.indexOf('    function openModal('));

function editor() {
  const ids = ['profileBasicsPanel', 'profileScalingPanel', 'profileAddonsPanel',
    'profileReshadePanel', 'profileRtssPanel', 'profileSpecialKPanel'];
  function element(id) {
    return {id, events: {}, attributes: {}, dataset: {}, open: false,
      classList: {contains: name => name === 'profile-major-panel'},
      addEventListener(type, handler) { (this.events[type] ||= []).push(handler); },
      setAttribute(name, value) { this.attributes[name] = value; },
      removeAttribute(name) { delete this.attributes[name]; },
      focus(options) { this.focused = options || true; },
      scrollIntoView(options) { this.scrolled = options; },
      getBoundingClientRect() { return {top: this.top || 0, bottom: (this.top || 0) + 100, left: 0, right: 100}; },
    };
  }
  const panels = ids.map((id, index) => {
    const panel = element(id);
    panel.top = index * 300;
    panel.summary = element(`${id}-summary`);
    panel.querySelector = () => panel.summary;
    return panel;
  });
  const buttons = ids.map(id => Object.assign(element(`${id}-link`), {dataset: {profileSectionLink: id}}));
  const nav = element('profileSectionNav');
  nav.scrollLeft = 0;
  nav.querySelectorAll = () => buttons;
  const header = element('header');
  header.offsetHeight = 125;
  const content = Object.assign(element('content'), {scrollTop: 0, clientHeight: 800, scrollHeight: 2400});
  content.properties = {};
  content.style = {setProperty: (key, value) => { content.properties[key] = value; }};
  content.querySelector = () => header;
  content.querySelectorAll = () => panels;
  const modal = element('profileModal');
  modal.classList.contains = () => true;
  const nodes = Object.fromEntries([...panels, nav, modal].map(node => [node.id, node]));
  const context = vm.createContext({
    document: {getElementById: id => nodes[id], querySelector: () => content},
    requestAnimationFrame: action => action(),
  });
  vm.runInContext(code, context);
  context.initializeProfileSections();
  return {context, panels, buttons, content, header};
}

test('header navigation opens a collapsed panel before scrolling and focusing its heading', () => {
  const {panels, buttons, content} = editor();
  assert.deepEqual(panels.map(panel => panel.open), [true, true, false, false, false, false]);
  buttons[2].events.click[0]();
  assert.equal(panels[2].open, true);
  assert.equal(panels[0].open, true);
  assert.equal(panels[2].scrolled.block, 'start');
  assert.equal(panels[2].summary.focused.preventScroll, true);
  assert.equal(buttons[2].attributes['aria-current'], 'location');
  assert.equal(buttons[2].attributes['aria-expanded'], 'true');
  assert.equal(buttons[0].attributes['aria-current'], undefined);
  assert.equal(content.properties['--profile-header-height'], '125px');
});

test('arrow, Home and End keys move navigation focus without changing panel settings', () => {
  const {panels, buttons} = editor();
  let prevented = 0;
  for (const [index, key, target] of [[0, 'ArrowLeft', 5], [5, 'ArrowRight', 0], [1, 'End', 5], [4, 'Home', 0]]) {
    buttons[index].events.keydown[0]({key, preventDefault() { prevented++; }});
    assert.ok(buttons[target].focused);
  }
  assert.equal(prevented, 4);
  assert.deepEqual(panels.map(panel => panel.open), [true, true, false, false, false, false]);
});

test('invalid settings reveal all enclosing disclosures and update the navigation', () => {
  const {context, panels, buttons} = editor();
  panels[2].tagName = 'DETAILS';
  const nested = {tagName: 'DETAILS', open: false, parentElement: panels[2], classList: {contains: () => false}};
  context.revealProfileSetting({parentElement: nested});
  assert.equal(nested.open, true);
  assert.equal(panels[2].open, true);
  assert.equal(buttons[2].attributes['aria-current'], 'location');
});

test('scroll tracking follows the section beneath the sticky header and the last open panel', () => {
  const {panels, buttons, content} = editor();
  panels.forEach((panel, index) => { panel.top = (index - 2) * 300; });
  content.scrollTop = 600;
  content.events.scroll[0]();
  assert.equal(buttons[2].attributes['aria-current'], 'location');
  panels[5].open = true;
  content.scrollTop = content.scrollHeight - content.clientHeight;
  content.events.scroll[0]();
  assert.equal(buttons[5].attributes['aria-current'], 'location');
});

test('reopening a profile resets its view without duplicating event handlers', () => {
  const {context, panels, buttons, content} = editor();
  buttons[5].events.click[0]();
  context.initializeProfileSections();
  assert.equal(panels[5].open, false);
  assert.equal(content.scrollTop, 0);
  assert.equal(buttons[0].attributes['aria-current'], 'location');
  assert.equal(buttons[0].events.click.length, 1);
  assert.equal(content.events.scroll.length, 1);
});

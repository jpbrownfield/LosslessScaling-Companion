const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function background() {
  let loadSettings, changeSettings, receiveMessage;
  let timerId = 0;
  const sockets = [], intervals = new Map(), timeouts = new Map(), writes = [];
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    close() { this.readyState = 2; }
    send(message) { this.sent.push(JSON.parse(message)); }
    open() { this.readyState = 1; this.onopen(); }
  }
  const context = vm.createContext({
    console: { log() {}, warn() {}, error() {} }, WebSocket: Socket,
    setInterval(fn) { intervals.set(++timerId, fn); return timerId; },
    clearInterval(id) { intervals.delete(id); },
    setTimeout(fn) { timeouts.set(++timerId, fn); return timerId; },
    clearTimeout(id) { timeouts.delete(id); },
    chrome: {
      storage: {
        local: { get(defaults, callback) { loadSettings = callback; }, set(value) { writes.push(value); } },
        onChanged: { addListener(callback) { changeSettings = callback; } },
      },
      action: { setBadgeText() {}, setBadgeBackgroundColor() {} },
      runtime: { onMessage: { addListener(callback) { receiveMessage = callback; } } },
    },
  });
  vm.runInContext(fs.readFileSync('extension/background.js', 'utf8'), context);
  return {
    sockets, intervals, timeouts, writes,
    load(settings = {}) { loadSettings(settings); },
    changePort(port) { changeSettings({ wsPort: { newValue: port } }, 'local'); },
    reconnect() { receiveMessage({ action: 'RECONNECT' }, {}, () => {}); },
    connected() { return vm.runInContext('isConnected', context); },
  };
}

test('startup waits for saved settings and uses the saved port on the first connection', () => {
  const worker = background();
  assert.equal(worker.sockets.length, 0);
  worker.load({ wsPort: 24893 });
  assert.equal(worker.sockets.length, 1);
  assert.equal(worker.sockets[0].url, 'ws://127.0.0.1:24893/ws');
});

for (const action of ['reconnect', 'port change']) {
  test(`${action} ignores stale close, open, and message events`, () => {
    const worker = background();
    worker.load();
    const old = worker.sockets[0];
    old.open();
    if (action === 'reconnect') worker.reconnect();
    else worker.changePort(24894);
    const current = worker.sockets[1];
    assert.equal(worker.connected(), false);
    current.open();
    const writes = worker.writes.length;
    old.onclose();
    old.onopen();
    old.onmessage({ data: JSON.stringify({ type: 'STATE_UPDATE', isScalingActive: true }) });
    assert.equal(worker.connected(), true);
    assert.equal(worker.writes.length, writes);
    assert.equal(worker.timeouts.size, 0);
    assert.equal(worker.intervals.size, 1);
    for (const heartbeat of worker.intervals.values()) heartbeat();
    assert.equal(current.sent[0].type, 'PING');
    assert.equal(old.sent.length, 0);
    current.onclose();
    assert.equal(worker.connected(), false);
    assert.equal(worker.intervals.size, 0);
    assert.equal(worker.timeouts.size, 1);
  });
}

test('a stale close while the replacement is connecting does not schedule another connection', () => {
  const worker = background();
  worker.load();
  const old = worker.sockets[0];
  worker.reconnect();
  old.onclose();
  assert.equal(worker.timeouts.size, 0);
  worker.sockets[1].open();
  assert.equal(worker.connected(), true);
});

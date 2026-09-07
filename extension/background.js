/**
 * Lossless Scaling Bridge - Background Service Worker
 * Manages WebSocket connection to local Python companion server.
 */

const DEFAULT_SETTINGS = {
  enabled: true,
  wsPort: 24892,
  onlyForVideos: true,
  autoScaleOnEnter: true,
  autoScaleOnExit: false,
  browserProcess: 'chrome.exe',
  serverState: null
};

let socket = null;
let isConnected = false;
let reconnectTimer = null;
let heartbeatTimer = null;
let cachedSettings = { ...DEFAULT_SETTINGS };

// Load settings
chrome.storage.local.get(DEFAULT_SETTINGS, (items) => {
  cachedSettings = items;
  connectWebSocket();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local') {
    for (const key in changes) {
      cachedSettings[key] = changes[key].newValue;
    }
    if (changes.wsPort && socket) {
      socket.close();
      connectWebSocket();
    }
  }
});

function connectWebSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  clearTimeout(reconnectTimer);
  clearInterval(heartbeatTimer);

  const wsUrl = `ws://127.0.0.1:${cachedSettings.wsPort}/ws`;
  console.log(`[LS Bridge] Connecting to ${wsUrl}...`);

  try {
    socket = new WebSocket(wsUrl);

    socket.onopen = () => {
      console.log('[LS Bridge] Connected to Python companion.');
      isConnected = true;
      updateConnectionStatus(true);

      // Start heartbeat
      heartbeatTimer = setInterval(() => {
        if (socket && socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'PING', timestamp: Date.now() }));
        }
      }, 15000);
    };

    socket.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        handleServerMessage(msg);
      } catch (err) {
        console.error('[LS Bridge] Failed to parse message:', err);
      }
    };

    socket.onclose = () => {
      console.log('[LS Bridge] Disconnected. Retrying in 3s...');
      isConnected = false;
      updateConnectionStatus(false);
      clearInterval(heartbeatTimer);
      reconnectTimer = setTimeout(connectWebSocket, 3000);
    };

    socket.onerror = (err) => {
      console.warn('[LS Bridge] WebSocket error:', err);
      // onclose will trigger next
    };
  } catch (err) {
    console.error('[LS Bridge] Connection init failed:', err);
    isConnected = false;
    updateConnectionStatus(false);
    reconnectTimer = setTimeout(connectWebSocket, 3000);
  }
}

function updateConnectionStatus(connected) {
  chrome.storage.local.set({ isConnected: connected });
  chrome.action.setBadgeText({ text: connected ? 'ON' : 'OFF' });
  chrome.action.setBadgeBackgroundColor({ color: connected ? '#10b981' : '#ef4444' });
}

function sendToServer(msg) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(msg));
    return true;
  }
  return false;
}

function handleServerMessage(msg) {
  if (msg.type === 'PONG') return;
  if (msg.type === 'INITIAL_STATE' || msg.type === 'STATE_UPDATE' || msg.type === 'FULL_DATA_UPDATE') {
    chrome.storage.local.set({
      serverState: {
        isScalingActive: !!msg.isScalingActive,
        activeProfile: msg.activeProfile || null,
        scalingTarget: msg.scalingTarget || null
      }
    });
  }
  console.log('[LS Bridge] Received from companion:', msg);
}

// Listen to content script and popup messages
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'FULLSCREEN_EVENT') {
    if (!cachedSettings.enabled) {
      sendResponse({ status: 'ignored_disabled' });
      return true;
    }

    const data = message.data;
    if (data.type === 'FULLSCREEN_ENTER' && !cachedSettings.autoScaleOnEnter) {
      sendResponse({ status: 'ignored_enter_disabled' });
      return true;
    }
    if (data.type === 'FULLSCREEN_EXIT' && !cachedSettings.autoScaleOnExit) {
      sendResponse({ status: 'ignored_exit_disabled' });
      return true;
    }
    if (cachedSettings.onlyForVideos && !data.isVideo) {
      console.log('[LS Bridge] Fullscreen ignored (no video element found)');
      sendResponse({ status: 'ignored_not_video' });
      return true;
    }

    const payload = {
      type: 'BROWSER_FULLSCREEN_EVENT',
      event: data.type, // FULLSCREEN_ENTER or FULLSCREEN_EXIT
      domain: data.domain,
      url: data.url,
      title: data.title,
      isVideo: data.isVideo,
      video: data.video,
      processName: cachedSettings.browserProcess || 'chrome.exe',
      timestamp: data.timestamp
    };

    const sent = sendToServer(payload);
    sendResponse({ status: sent ? 'sent' : 'companion_offline' });
    return true;
  }

  if (message.action === 'MANUAL_TRIGGER') {
    const sent = sendToServer({
      type: 'MANUAL_TRIGGER',
      action: message.command || 'TOGGLE_SCALE',
      timestamp: Date.now()
    });
    sendResponse({ status: sent ? 'sent' : 'companion_offline' });
    return true;
  }

  if (message.action === 'GET_STATUS') {
    sendResponse({ isConnected, settings: cachedSettings });
    return true;
  }

  if (message.action === 'RECONNECT') {
    if (socket) socket.close();
    connectWebSocket();
    sendResponse({ status: 'reconnecting' });
    return true;
  }
});

// Auto-connect on startup / service worker wakeup
connectWebSocket();

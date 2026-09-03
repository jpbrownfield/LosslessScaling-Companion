document.addEventListener('DOMContentLoaded', () => {
  const statusBadge = document.getElementById('statusBadge');
  const enableToggle = document.getElementById('enableToggle');
  const onlyVideosToggle = document.getElementById('onlyVideosToggle');
  const triggerBtn = document.getElementById('triggerBtn');
  const reconnectBtn = document.getElementById('reconnectBtn');

  function updateUI() {
    chrome.runtime.sendMessage({ action: 'GET_STATUS' }, (response) => {
      if (!response) return;
      const { isConnected, settings } = response;

      if (isConnected) {
        statusBadge.textContent = 'Connected';
        statusBadge.className = 'badge connected';
      } else {
        statusBadge.textContent = 'Offline';
        statusBadge.className = 'badge disconnected';
      }

      if (settings) {
        enableToggle.checked = !!settings.enabled;
        onlyVideosToggle.checked = !!settings.onlyForVideos;
      }
    });
  }

  // Initial load
  updateUI();
  const pollInterval = setInterval(updateUI, 2000);
  window.addEventListener('unload', () => clearInterval(pollInterval));

  // Toggles
  enableToggle.addEventListener('change', (e) => {
    chrome.storage.local.set({ enabled: e.target.checked });
  });

  onlyVideosToggle.addEventListener('change', (e) => {
    chrome.storage.local.set({ onlyForVideos: e.target.checked });
  });

  // Manual Trigger
  triggerBtn.addEventListener('click', () => {
    triggerBtn.disabled = true;
    triggerBtn.textContent = 'Triggering...';
    chrome.runtime.sendMessage({ action: 'MANUAL_TRIGGER', command: 'TOGGLE_SCALE' }, (res) => {
      setTimeout(() => {
        triggerBtn.disabled = false;
        triggerBtn.innerHTML = '<span>⚡</span> Toggle Scaling Now';
      }, 500);
    });
  });

  // Reconnect
  reconnectBtn.addEventListener('click', () => {
    reconnectBtn.disabled = true;
    reconnectBtn.textContent = 'Reconnecting...';
    chrome.runtime.sendMessage({ action: 'RECONNECT' }, () => {
      setTimeout(() => {
        reconnectBtn.disabled = false;
        reconnectBtn.innerHTML = '<span>🔄</span> Reconnect Companion';
        updateUI();
      }, 1000);
    });
  });
});

/**
 * Lossless Scaling Bridge - Content Script
 * Monitors fullscreen transitions and video playback elements.
 */

(function () {
  let isFullscreenActive = false;
  let debounceTimer = null;

  function findActiveVideo(fullscreenEl) {
    if (!fullscreenEl) return null;
    if (fullscreenEl.tagName === 'VIDEO') return fullscreenEl;
    return fullscreenEl.querySelector('video');
  }

  function getVideoMetadata(videoEl) {
    if (!videoEl) return null;
    return {
      videoWidth: videoEl.videoWidth || 0,
      videoHeight: videoEl.videoHeight || 0,
      duration: videoEl.duration || 0,
      paused: videoEl.paused,
      src: videoEl.currentSrc || videoEl.src || ''
    };
  }

  function handleFullscreenChange() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      const fsElement = document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement;
      const isNowFullscreen = !!fsElement;

      if (isNowFullscreen === isFullscreenActive) {
        return;
      }

      isFullscreenActive = isNowFullscreen;
      const videoEl = findActiveVideo(fsElement);
      const videoMeta = getVideoMetadata(videoEl);

      const eventPayload = {
        type: isNowFullscreen ? 'FULLSCREEN_ENTER' : 'FULLSCREEN_EXIT',
        url: window.location.href,
        domain: window.location.hostname,
        title: document.title,
        isVideo: !!videoEl,
        video: videoMeta,
        timestamp: Date.now()
      };

      try {
        chrome.runtime.sendMessage({
          action: 'FULLSCREEN_EVENT',
          data: eventPayload
        });
      } catch (err) {
        // Context might be invalidated on extension reload
        console.debug('[LS Bridge] Message send error:', err);
      }
    }, 150); // 150ms debounce for smoother transitions
  }

  // Listen to all standard fullscreen events
  document.addEventListener('fullscreenchange', handleFullscreenChange);
  document.addEventListener('webkitfullscreenchange', handleFullscreenChange);
  document.addEventListener('mozfullscreenchange', handleFullscreenChange);
  document.addEventListener('MSFullscreenChange', handleFullscreenChange);

  // Notify background on script load for initialization
  try {
    chrome.runtime.sendMessage({ action: 'CONTENT_READY', url: window.location.href });
  } catch (_) {}
})();

'use strict';

// macOS draws system traffic lights in the top-left of a single 52px title bar.
// The renderer reserves a 72px vertical nav; the lights sit in that bar, centered.
// Windows keeps the native caption overlay.
const MAC_NAV_WIDTH = 72;
const MAC_TITLEBAR_HEIGHT = 52;

function windowChrome(platform) {
  const chrome = { titleBarStyle: 'hidden' };
  if (platform === 'darwin') {
    chrome.trafficLightPosition = { x: 12, y: 19 };
    return chrome;
  }
  chrome.titleBarOverlay = { color: '#f5f7f5', symbolColor: '#334139', height: 56 };
  return chrome;
}

module.exports = { windowChrome, MAC_NAV_WIDTH, MAC_TITLEBAR_HEIGHT };

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { windowChrome, MAC_NAV_WIDTH, MAC_TITLEBAR_HEIGHT } = require('../electron/window-chrome.cjs');

test('windows keeps the caption overlay and does not move traffic lights', () => {
  const chrome = windowChrome('win32');
  assert.equal(chrome.titleBarStyle, 'hidden');
  assert.deepEqual(chrome.titleBarOverlay, { color: '#f5f7f5', symbolColor: '#334139', height: 56 });
  assert.equal(chrome.trafficLightPosition, undefined);
});

test('macos places traffic lights inside the vertical nav and skips the windows overlay', () => {
  const chrome = windowChrome('darwin');
  assert.equal(chrome.titleBarStyle, 'hidden');
  assert.equal(chrome.titleBarOverlay, undefined);
  assert.equal(MAC_NAV_WIDTH, 72);
  assert.equal(MAC_TITLEBAR_HEIGHT, 52);
  const lights = chrome.trafficLightPosition;
  assert.equal(lights.x, 12);
  assert.equal(lights.y, 19);
  assert.ok(lights.x + 54 <= MAC_NAV_WIDTH);
  assert.ok(lights.y >= 16 && lights.y + 14 <= MAC_TITLEBAR_HEIGHT);
});

test('renderer keeps the windows toolbar and adds a darwin sidebar', () => {
  const app = fs.readFileSync(path.join(__dirname, '../src/App.tsx'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../src/workspace-theme.css'), 'utf8');
  const preload = fs.readFileSync(path.join(__dirname, '../electron/preload.cjs'), 'utf8');
  assert.match(preload, /platform:\s*process\.platform/);
  assert.match(app, /platform === 'darwin'/);
  assert.match(app, /className="mac-sidebar"/);
  assert.match(app, /className="window-controls-space"/);
  assert.match(app, /className="rail-nav"/);
  assert.match(css, /html\[data-platform="darwin"\] \.mac-sidebar \{[^}]*flex-direction:\s*column/);
  assert.match(css, /padding:\s*52px 0 12px/);
  assert.match(css, /html\[data-platform="darwin"\] \.app-toolbar \{[^}]*height:\s*52px;/);
  assert.doesNotMatch(css, /grid-template-rows:\s*40px/);
  assert.match(css, /html\[data-platform="darwin"\] \.app-toolbar \{[^}]*grid-template-columns:\s*var\(--sidebar-width\) minmax\(0,\s*1fr\);/);
  const styles = fs.readFileSync(path.join(__dirname, '../src/styles.css'), 'utf8');
  assert.match(styles, /\.app-toolbar \{[^}]*138px/);
  assert.match(styles, /\.window-controls-space \{[^}]*height:\s*56px/);
});

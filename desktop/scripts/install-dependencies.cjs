'use strict';
require('./node-trust.cjs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const desktop = path.resolve(__dirname, '..');
const result = spawnSync(process.platform === 'win32' ? 'npm.cmd' : 'npm',
  ['ci', '--no-audit', '--no-fund'],
  {cwd: desktop, stdio: 'inherit', shell: process.platform === 'win32', env: process.env});
if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);
// Electron 44 does not install its runtime during npm ci. Download the pinned,
// checksum-verified runtime explicitly, using the same audited build CA bundle.
const electron = spawnSync(process.execPath,
  [path.join(desktop, 'node_modules/electron/install.js')],
  {cwd: desktop, stdio: 'inherit', env: process.env});
if (electron.error) throw electron.error;
process.exit(electron.status ?? 1);

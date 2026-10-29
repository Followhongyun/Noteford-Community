'use strict';
const fs = require('node:fs');
const path = require('node:path');

// Modification to electron-builder's MIT-licensed template, 2026-10-11:
// omit optional shortcut metadata and shell-history/unpin calls from WinShell.dll,
// whose upstream distribution provides no clear redistribution license.
// Ordinary shortcut deletion and the updater's installer contract are retained.
module.exports = function prepare() {
  const base = path.dirname(require.resolve('app-builder-lib/package.json'));
  const version = JSON.parse(fs.readFileSync(path.join(base, 'package.json'))).version;
  if (version !== '26.15.3') throw Error('Review installer template patch for new electron-builder version');
  for (const [relative, count] of [['uninstaller.nsh', 3], ['include/installer.nsh', 7]]) {
    const file = path.join(base, 'templates/nsis', relative);
    const source = fs.readFileSync(file, 'utf8');
    const calls = source.match(/^\s*WinShell::.*$/gm) || [];
    if (calls.length === 0 && (source.match(/Noteford: omitted unlicensed WinShell call/g) || []).length === count) continue;
    if (calls.length !== count) throw Error('Unexpected WinShell template; review before packaging');
    fs.writeFileSync(file, source.replace(/^\s*WinShell::.*$/gm,
      '\n    # Noteford: omitted unlicensed WinShell call; ordinary shortcuts remain.'));
  }
};

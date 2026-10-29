'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

function isAlive(pid, executable) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); } catch (error) { return error.code !== 'ESRCH'; }
  if (process.platform === 'win32' && typeof executable === 'string') {
    try {
      const actual = execFileSync('powershell.exe', ['-NoProfile', '-Command', `(Get-Process -Id ${pid} -ErrorAction Stop).Path`], {encoding:'utf8', windowsHide:true, timeout:3000}).trim();
      if (actual) return path.resolve(actual).toLowerCase() === path.resolve(executable).toLowerCase();
    } catch { /* Inaccessible live processes remain protected. */ }
  }
  return true;
}

function publishDiscovery({ directory, command, config, appExecutable, pid, skillPath, args, alive = isAlive, interval = 5000 }) {
  const file = path.join(directory, 'connection.json');
  const instance = crypto.randomUUID();
  const record = { version: 1, product: 'Noteford Community', instance, pid, appExecutable, command, config, skillPath };
  if (Array.isArray(args) && args.length && args.every(item => typeof item === 'string')) record.args = args;
  let stopped = false;
  function refresh() {
    if (stopped) return;
    try {
      const current = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (current.instance === instance && JSON.stringify(current) === JSON.stringify(record)) return;
      // Never compete with another live instance; recover only missing/stale records.
      if (current.instance !== instance && alive(current.pid, current.appExecutable)) return;
    } catch (error) {
      if (!(error instanceof SyntaxError) && error.code !== 'ENOENT') throw error;
    }
    fs.mkdirSync(directory, { recursive: true });
    const temporary = `${file}.${instance}.tmp`;
    try {
      fs.writeFileSync(temporary, JSON.stringify(record, null, 2), { flag: 'w' });
      fs.renameSync(temporary, file);
    } finally {
      try { fs.unlinkSync(temporary); } catch { /* rename already removed it */ }
    }
  }
  refresh();
  const timer = setInterval(() => { try { refresh(); } catch { /* retry on next tick after transient file lock */ } }, interval);
  timer.unref();
  const cleanup = () => {
    stopped = true;
    clearInterval(timer);
    try {
      if (JSON.parse(fs.readFileSync(file, 'utf8')).instance === instance) fs.unlinkSync(file);
    } catch { /* stale records are rejected by the Skill process check */ }
  };
  cleanup.refresh = refresh;
  return cleanup;
}
module.exports = { publishDiscovery };

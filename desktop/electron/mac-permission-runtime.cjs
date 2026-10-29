'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');

// Optional, read-only SPI: no public API exposes the FDA switch before access
// takes effect. Treat this only as a restart hint, never as proof of access.
// WebKit's TCCSPI.h defines kTCCAccessPreflightGranted as 0. Missing symbols,
// other return values and timeouts must all degrade to unknown.
function createFullDiskPolicyReader({ load = () => {
  const koffi = require('koffi');
  const cf = koffi.load('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation');
  const tcc = koffi.load('/System/Library/PrivateFrameworks/TCC.framework/TCC');
  const make = cf.func('void *CFStringCreateWithCString(void *, const char *, uint32_t)');
  const release = cf.func('void CFRelease(void *)');
  const preflight = promisify(tcc.func('int TCCAccessPreflight(const void *, const void *)').async);
  return async () => {
    const service = make(null, 'kTCCServiceSystemPolicyAllFiles', 0x08000100);
    if (!service) return null;
    try { return await preflight(service, null); } finally { release(service); }
  };
} } = {}) {
  let probe;
  let pending;
  return async ({ timeoutMs = 300 } = {}) => {
    if (!pending) {
      pending = Promise.resolve().then(() => {
        probe ??= load();
        return probe();
      }).then(code => code === 0 ? true : code === 1 ? false : null).catch(() => null);
      pending.finally(() => { pending = null; });
    }
    let timer;
    try {
      return await Promise.race([pending, new Promise(resolve => { timer = setTimeout(() => resolve(null), timeoutMs); })]);
    } finally { clearTimeout(timer); }
  };
}

function parseSignature(output = '') {
  return {
    identifier: /^Identifier=(.+)$/m.exec(output)?.[1] || '',
    cdhash: /^CDHash=([a-f0-9]+)$/mi.exec(output)?.[1] || '',
    adhoc: /^Signature=adhoc$/m.test(output),
    hashRequirement: /designated =>.*cdhash/.test(output),
  };
}

function signatureGuidance(signature, previous = {}) {
  if (!signature || signature.identifier !== 'com.followhongyun.noteford') {
    return '无法确认当前 Noteford 的签名。请使用「应用程序」中的安装版重新授权。';
  }
  if (!signature.adhoc) return '';
  const changed = previous.cdhash && signature.cdhash && previous.cdhash !== signature.cdhash;
  const reason = changed ? '检测到 Noteford 签名已变化，旧授权可能失效。' : '此版本使用临时签名，更新后旧授权可能失效。';
  return reason + '若出现多条 Noteford，请移除旧项，为当前应用重新授权。';
}

async function readSignatureNotice({ bundle, marker, run = promisify(execFile), io = fs } = {}) {
  try {
    const { stdout, stderr } = await run('/usr/bin/codesign', ['-d', '--verbose=4', '-r-', bundle], { timeout: 1500 });
    const signature = parseSignature(`${stdout}\n${stderr}`);
    let previous = {};
    try { previous = JSON.parse(io.readFileSync(marker, 'utf8')); } catch { /* first launch */ }
    const notice = signatureGuidance(signature, previous);
    if (signature.cdhash) {
      try {
        io.mkdirSync(path.dirname(marker), { recursive: true });
        io.writeFileSync(marker, JSON.stringify(signature));
      } catch { /* diagnostics must not prevent checking permissions */ }
    }
    return notice;
  } catch { return signatureGuidance(null); }
}

// Share each measurement across IPC/poll/focus callers. A focus during a check
// queues another measurement so a pre-toggle result cannot win the race.
function createPermissionMonitor({ read, publish = () => {} }) {
  let pending;
  let dirty = false;
  let revision = 0;
  function refresh({ force = false } = {}) {
    if (pending) { dirty ||= force; return pending; }
    pending = (async () => {
      let guide;
      do {
        dirty = false;
        guide = { ...await read(), revision: ++revision };
        publish(guide);
      } while (dirty);
      return guide;
    })().finally(() => { pending = null; });
    return pending;
  }
  return { refresh };
}

function bindPermissionFocus(app, refresh) {
  const focus = () => { void refresh({ force: true }).catch(() => {}); };
  for (const event of ['browser-window-focus', 'did-become-active']) app.on(event, focus);
  return () => {
    for (const event of ['browser-window-focus', 'did-become-active']) app.removeListener(event, focus);
  };
}

function restartForPermissions(app, platform, { requestRestart } = {}) {
  if (platform !== 'darwin') return { ok: false };
  // Source mode must keep Vite and file watchers alive while Electron exits.
  if (requestRestart) return { ok: requestRestart() !== false };
  app.relaunch();
  app.quit();
  return { ok: true };
}

module.exports = {
  createFullDiskPolicyReader, parseSignature, signatureGuidance, readSignatureNotice,
  createPermissionMonitor, bindPermissionFocus, restartForPermissions,
};

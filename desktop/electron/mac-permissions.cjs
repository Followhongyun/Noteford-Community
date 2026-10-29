'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');
const {
  SETTINGS_GUIDANCE, SETTINGS_URLS, GRANT_VISIBLE_MS,
  DIALOG_GUIDANCE, OPEN_WECHAT_GUIDANCE,
  automationCardStatus, automationSettingsGuidance,
  executePermissionGrant, FDA_GUIDANCE,
} = require('./permission-guide.cjs');

const BUNDLE_ID = 'com.followhongyun.noteford';
const MISSING_TARGET = new Set([-600, -10814]);

function appBundlePath(execPath = '') {
  const marker = `${path.sep}Contents${path.sep}MacOS${path.sep}`;
  const index = String(execPath).lastIndexOf(marker);
  if (index <= 0) return '';
  return String(execPath).slice(0, index);
}

function packagedModulePath(resolved, exists = fs.existsSync) {
  const value = String(resolved || '');
  const marker = `${path.sep}app.asar${path.sep}`;
  const index = value.indexOf(marker);
  if (index < 0) return value;
  const unpacked = `${value.slice(0, index)}${path.sep}app.asar.unpacked${path.sep}${value.slice(index + marker.length)}`;
  return exists(unpacked) ? unpacked : value;
}

function resolveKoffi() {
  try { return packagedModulePath(require.resolve('koffi')); } catch { return ''; }
}

function completeAutomationResults(rows) {
  const codes = new Map((rows || []).map(row => [
    `${row.target}:${row.event}`, Number.isInteger(row.code) ? row.code : -1744,
  ]));
  const system = codes.has('system_events:getd') ? codes.get('system_events:getd') : -1744;
  const wechatGet = codes.has('wechat:getd') ? codes.get('wechat:getd') : (codes.has('wechat:quit') ? codes.get('wechat:quit') : -1744);
  const wechatQuit = codes.has('wechat:quit') ? codes.get('wechat:quit') : wechatGet;
  return [
    { target: 'system_events', event: 'getd', code: system },
    { target: 'wechat', event: 'getd', code: wechatGet },
    { target: 'wechat', event: 'quit', code: wechatQuit },
  ];
}

// Opening applications uses LaunchServices; it never sends the consent event.
// Only System Events is started here. The user must open WeChat themselves.
function prepareAutomationTarget(bundleId) {
  if (bundleId !== 'com.apple.systemevents') return Promise.resolve();
  return new Promise(resolve => {
    execFile('/usr/bin/open', ['-g', '-b', bundleId], { timeout: 500 }, () => resolve());
  });
}

function createAutomationController({
  runChecks = require('./automation-check.cjs').runChecks,
  koffiPath,
  prepareTarget = prepareAutomationTarget,
} = {}) {
  let prompt = null;
  let reading = null;
  const run = ask => Promise.resolve().then(() => runChecks({
    ask, koffiPath: koffiPath === undefined ? resolveKoffi() : koffiPath, prepareTarget,
  })).then(results => ({ ok: true, results: completeAutomationResults(results) }))
    .catch(() => ({ ok: false, results: [], error: 'checker_unavailable' }));
  return {
    request() {
      // A system dialog cannot safely be cancelled by freeing its descriptors.
      // Repeated clicks reuse it until native completion, preventing a backlog.
      if (!prompt) {
        const active = { done: run(true) };
        prompt = active;
        active.done.finally(() => { if (prompt === active) prompt = null; });
      }
      return prompt;
    },
    async read({ timeoutMs = 300 } = {}) {
      if (!reading) {
        reading = run(false);
        reading.finally(() => { reading = null; });
      }
      const settled = await awaitPrompt({ done: reading }, timeoutMs);
      return settled.pending
        ? { ok: false, results: [], timedOut: true }
        : { ...settled.result, timedOut: false };
    },
  };
}

const controller = createAutomationController();
function requestAutomationConsent() { return controller.request(); }
function readAutomationStatus(options) { return controller.read(options); }

function revealAppArguments(execPath, exists = fs.existsSync) {
  const bundle = appBundlePath(execPath);
  const installed = '/Applications/Noteford.app';
  if (exists(installed)) return ['-R', installed];
  // Reveal the running Noteford copy, including a nonstandard install location.
  // A dev Electron executable must never be offered as Noteford for FDA.
  if (bundle && path.basename(bundle) === 'Noteford.app') return ['-R', bundle];
  return null;
}

function presentFullDiskGrant({ current, execPath, exists, openSettings, revealApp } = {}) {
  const result = executePermissionGrant({ platform: 'darwin', id: 'full_disk', current, openSettings });
  if (result.openedSettings) {
    const args = revealAppArguments(execPath, exists);
    if (args) {
      try { revealApp(args[1]); } catch { /* manual + selection remains available */ }
    }
    result.guidance = FDA_GUIDANCE;
  }
  return result;
}

function awaitPrompt(prompt, visibleMs) {
  if (prompt && prompt.done && typeof prompt.done.then === 'function') {
    let timer;
    const budget = new Promise(resolve => {
      timer = setTimeout(() => resolve({ pending: true }), visibleMs);
    });
    return Promise.race([
      Promise.resolve(prompt.done).then(result => {
        clearTimeout(timer);
        return { pending: false, result };
      }),
      budget,
    ]).finally(() => clearTimeout(timer));
  }
  return Promise.resolve({ pending: false, result: prompt || {} });
}

async function presentAutomationGrant({
  current = 'unknown',
  readStatus,
  wechatRunning = true,
  startPrompt,
  openSettings,
  lowerWindow = () => {},
  revealWindow = () => {},
  visibleMs = GRANT_VISIBLE_MS,
} = {}) {
  if (current === 'granted') {
    return { ok: true, id: 'automation', status: 'granted', openedSettings: false, prompted: false, guidance: '', timedOut: false };
  }
  if (!wechatRunning) {
    return { ok: true, id: 'automation', status: 'not_granted', openedSettings: false, prompted: false, guidance: OPEN_WECHAT_GUIDANCE };
  }
  try {
    lowerWindow();
  } catch {
    try { revealWindow(false); } catch { /* keep the card usable */ }
    try { openSettings(SETTINGS_URLS.automation); } catch { /* the card still leaves the spinner */ }
    return {
      ok: true,
      id: 'automation',
      status: 'not_granted',
      openedSettings: true,
      prompted: true,
      guidance: SETTINGS_GUIDANCE,
      timedOut: true,
    };
  }
  let prompt;
  try {
    prompt = typeof startPrompt === 'function' ? startPrompt() : null;
    const settled = await awaitPrompt(prompt, visibleMs);
    if (settled.pending) {
      const later = prompt && prompt.done;
      if (later && typeof later.then === 'function') {
        later.then(value => {
          const rows = value && Array.isArray(value.results) ? completeAutomationResults(value.results) : null;
          const grantedLater = rows && automationCardStatus(rows) === 'granted';
          try { revealWindow(grantedLater); } catch { /* the dialog already closed */ }
        }).catch(() => {
          try { revealWindow(false); } catch { /* already hidden */ }
        });
      }
      try { revealWindow(false); } catch { /* keep retry visible */ }
      try { openSettings(SETTINGS_URLS.automation); } catch { /* guidance remains visible */ }
      return {
        ok: true,
        id: 'automation',
        status: 'not_granted',
        openedSettings: true,
        prompted: true,
        guidance: DIALOG_GUIDANCE,
        timedOut: true,
      };
    }
    const value = settled.result || {};
    const rows = Array.isArray(value.results) ? completeAutomationResults(value.results) : null;
    if (rows && MISSING_TARGET.has(Number(rows.find(row => row.target === 'wechat')?.code))) {
      try { revealWindow(false); } catch { /* the card still says to open WeChat */ }
      try { openSettings(SETTINGS_URLS.automation); } catch { /* retry guidance remains visible */ }
      return {
        ok: true,
        id: 'automation',
        status: 'not_granted',
        openedSettings: true,
        prompted: true,
        guidance: OPEN_WECHAT_GUIDANCE,
        timedOut: false,
      };
    }
    let granted = rows ? automationCardStatus(rows) === 'granted' : false;
    if (!rows && value.ok && typeof readStatus === 'function') {
      const again = await readStatus();
      granted = !!(again && again.status === 'granted');
    }
    if (granted) {
      try { if (prompt && prompt.cancel) prompt.cancel(); } catch { /* the dialog already finished */ }
      try { revealWindow(true); } catch { /* the card still reports granted */ }
      return { ok: true, id: 'automation', status: 'granted', openedSettings: false, prompted: true, guidance: '', timedOut: false };
    }
    try { revealWindow(false); } catch { /* settings still has to open */ }
    try { openSettings(SETTINGS_URLS.automation); } catch { /* the card still names the switch */ }
    return {
      ok: true,
      id: 'automation',
      status: 'not_granted',
      openedSettings: true,
      prompted: true,
      guidance: automationSettingsGuidance(rows || []),
      timedOut: false,
    };
  } catch {
    try { if (prompt && prompt.cancel) prompt.cancel(); } catch { /* already gone */ }
    try { revealWindow(false); } catch { /* do not leave the window hidden */ }
    try { openSettings(SETTINGS_URLS.automation); } catch { /* the card still leaves the spinner */ }
    return {
      ok: true,
      id: 'automation',
      status: 'not_granted',
      openedSettings: true,
      prompted: true,
      guidance: SETTINGS_GUIDANCE,
      timedOut: true,
    };
  }
}

module.exports = {
  BUNDLE_ID, appBundlePath, packagedModulePath, completeAutomationResults,
  createAutomationController, requestAutomationConsent, readAutomationStatus,
  revealAppArguments, presentFullDiskGrant, presentAutomationGrant,
};

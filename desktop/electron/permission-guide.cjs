'use strict';

// macOS permission guide. Windows has no cards and never prompts.
// Automation is required: confirm this Mac, and quit WeChat while preparing it.
// Full Disk Access is required to read WeChat's container. Camera, microphone,
// screen recording, and accessibility are not used, so they are not requested.

const DIALOG_SHOWN_MS = 700;
const GRANT_VISIBLE_MS = 1000;
const GRANT_PROMPT_TIMEOUT_MS = 8000;
const GRANT_UI_TIMEOUT_MS = 2500;
const SETTINGS_GUIDANCE = '请在系统设置里打开开关。';
const FDA_GUIDANCE = '点「+」添加访达中的 Noteford 开源版.app，打开开关后重启。';
const OPEN_WECHAT_GUIDANCE = '请先打开微信，再点一次「授权」。';
const DIALOG_GUIDANCE = '请在系统对话框里允许 Noteford 控制「系统事件」和「微信」。未出现对话框时，在已打开的自动化设置中检查开关；列表没有 Noteford 时返回重试。';

const SETTINGS_URLS = {
  automation: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Automation',
  full_disk: 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles',
};

const AUTOMATION_CHECKS = [
  { target: 'system_events', event: 'getd', bundleId: 'com.apple.systemevents', eventClass: '636F7265', eventId: '67657464' },
  { target: 'wechat', event: 'getd', bundleId: 'com.tencent.xinWeChat', eventClass: '636F7265', eventId: '67657464' },
  { target: 'wechat', event: 'quit', bundleId: 'com.tencent.xinWeChat', eventClass: '61657674', eventId: '71756974' },
];

const MAC_PERMISSIONS = [
  {
    id: 'automation',
    title: '自动化',
    detail: '用来确认本机状态，并在准备微信时退出微信。',
    required: true,
    prompts: true,
    settingsUrl: SETTINGS_URLS.automation,
  },
  {
    id: 'full_disk',
    title: '完全磁盘访问',
    detail: '用来只读读取微信里的聊天资料。',
    restartNote: '点「+」添加 Noteford，打开开关后重启。',
    required: true,
    prompts: false,
    settingsUrl: SETTINGS_URLS.full_disk,
  },
];

function interpretAutomationCode(code) {
  const value = Number.isInteger(code) ? code : NaN;
  if (value === 0) return 'granted';
  if (value === -1743) return 'denied';
  if (value === -1744) return 'not_determined';
  return 'unknown';
}

function foldSwitch(codes) {
  if (!codes || !codes.length) return 'unknown';
  if (codes.every(code => code === 'granted')) return 'granted';
  if (codes.some(code => code === 'denied') && !codes.some(code => code === 'granted')) return 'denied';
  if (codes.some(code => code === 'not_determined' || code === 'unknown')) return 'not_determined';
  if (codes.some(code => code === 'denied')) return 'denied';
  return 'unknown';
}

function automationCardStatus(results) {
  const grouped = { system_events: [], wechat: [] };
  for (const row of results || []) {
    if (!grouped[row.target]) continue;
    grouped[row.target].push(interpretAutomationCode(row.code));
  }
  const system = foldSwitch(grouped.system_events);
  const wechat = foldSwitch(grouped.wechat);
  if (system === 'granted' && wechat === 'granted') return 'granted';
  if ((results || []).some(row => row.target === 'wechat' && [-600, -10814].includes(row.code))) return 'wechat_not_running';
  if (system === 'granted') return 'missing_wechat';
  if (wechat === 'granted') return 'missing_system_events';
  if (system === 'denied' || wechat === 'denied') return 'denied';
  if (system === 'not_determined' || wechat === 'not_determined') return 'not_determined';
  return 'unknown';
}

function automationGapNote(status) {
  if (status === 'wechat_not_running') return '请先打开微信，再点「授权」。';
  if (status === 'missing_wechat') return '请在「系统设置 → 隐私与安全性 → 自动化」里，打开 Noteford 下面的「微信」。';
  if (status === 'missing_system_events') return '请在「系统设置 → 隐私与安全性 → 自动化」里，打开 Noteford 下面的「系统事件」。';
  return '';
}

function automationSettingsGuidance(results) {
  const status = automationCardStatus(results);
  if (status === 'granted') return '';
  const named = automationGapNote(status);
  if (named) return named;
  return '请在「系统设置 → 隐私与安全性 → 自动化」里，打开 Noteford 下面的「系统事件」和「微信」。';
}

function classifyReadError(error) {
  if (!error) return 'ok';
  const code = error.code || '';
  if (code === 'ENOENT' || code === 'ENOTDIR') return 'missing';
  if (code === 'EACCES' || code === 'EPERM') return 'denied';
  const message = String(error.message || '').toLowerCase();
  if (message.includes('operation not permitted') || message.includes('permission denied')) return 'denied';
  return 'error';
}

function fullDiskStatus(reads, { policyAllowed = false } = {}) {
  // Access to WeChat's container can be granted separately on recent macOS.
  // Only a protected FDA-only file proves the full-disk switch is effective.
  const proof = (reads || []).filter(row => row && ['tcc', 'tcc_system'].includes(row.id) && row.proves !== false);
  if (proof.some(row => row.result === 'ok')) return 'granted';
  // The policy signal alone NEVER turns the card green. The current process
  // must reopen and read the file after relaunch before it may continue.
  if (policyAllowed && proof.some(row => row.result === 'denied')) return 'restart_required';
  if (proof.some(row => row.result === 'denied')) return 'denied';
  return 'not_determined';
}

function fullDiskProbeResult({ kind, error, bytes = 0 } = {}) {
  if (error) return classifyReadError(error);
  if (kind === 'file') return Number(bytes) > 0 ? 'ok' : 'denied';
  if (kind === 'dir') return 'error';
  return 'error';
}

function statusesFromProbes({ automation, reads, policyAllowed = false } = {}) {
  return {
    automation: automation && automation.ok ? automationCardStatus(automation.results) : 'unknown',
    full_disk: fullDiskStatus(reads, { policyAllowed }),
  };
}

function permissionById(platform, id) {
  if (platform !== 'darwin') return null;
  return MAC_PERMISSIONS.find(item => item.id === id) || null;
}

function presentCard(definition, status) {
  const granted = status === 'granted';
  const restart = definition.id === 'full_disk' && status === 'restart_required';
  const required = definition.required !== false;
  return {
    id: definition.id,
    title: definition.title,
    detail: definition.detail,
    restartNote: granted ? '' : restart ? '需要重启 Noteford，重启后会再次检查。' : definition.restartNote || '',
    gapNote: definition.id === 'automation' ? automationGapNote(status) : '',
    required,
    optionalLabel: required ? '' : '可选',
    status: granted ? 'granted' : restart ? 'restart_required' : 'not_granted',
    actionLabel: granted ? '已授权' : restart ? '重启' : '授权',
  };
}

function permissionGuide({ platform, statuses = {} } = {}) {
  if (platform !== 'darwin') {
    return {
      active: false,
      loading: false,
      title: '',
      intro: '',
      cards: [],
      canContinue: true,
      continueLabel: '',
      hint: '',
      steps: [{ id: 'account', label: '账号' }],
    };
  }
  const cards = MAC_PERMISSIONS.map(definition => presentCard(definition, statuses[definition.id]));
  const canContinue = cards.every(card => !card.required || card.status === 'granted');
  return {
    active: true,
    loading: false,
    title: '需要权限',
    intro: '先允许这些权限，再选择微信账号。',
    cards,
    canContinue,
    continueLabel: '继续',
    hint: '按回车继续，或用左右方向键切换',
    steps: [
      { id: 'permissions', label: '权限' },
      { id: 'connect', label: '连接微信' },
    ],
  };
}

function loadingPermissionGuide() {
  const guide = permissionGuide({ platform: 'darwin', statuses: {} });
  return {
    ...guide,
    loading: true,
    intro: '正在确认本机权限…',
    cards: [],
    canContinue: false,
  };
}

function applyOnboardingKey({ platform, step, key, canContinue, connectCanContinue }) {
  if (platform !== 'darwin' || (step !== 'permissions' && step !== 'connect')) {
    return { step: step || 'account', handled: false, changed: false };
  }
  if (step === 'connect') {
    if (key === 'ArrowLeft') return { step: 'permissions', handled: true, changed: true };
    if (key === 'ArrowRight' || key === 'Enter') {
      if (!connectCanContinue) return { step: 'connect', handled: true, changed: false };
      return { step: 'account', handled: true, changed: true };
    }
    return { step: 'connect', handled: false, changed: false };
  }
  if (key === 'ArrowLeft') return { step: 'permissions', handled: true, changed: false };
  if (key === 'ArrowRight' || key === 'Enter') {
    if (!canContinue) return { step: 'permissions', handled: true, changed: false };
    return { step: 'connect', handled: true, changed: true };
  }
  return { step: 'permissions', handled: false, changed: false };
}

function checkKey(row) {
  return `${row.target}:${row.event}`;
}

function inferDialogShown({ before, after, elapsedMs }) {
  const prior = new Map((before || []).map(row => [checkKey(row), interpretAutomationCode(row.code)]));
  for (const row of after || []) {
    const was = prior.get(checkKey(row));
    const now = interpretAutomationCode(row.code);
    if ((was === 'not_determined' || was === 'unknown' || was === undefined) && (now === 'granted' || now === 'denied')) {
      return true;
    }
  }
  return Number(elapsedMs) >= DIALOG_SHOWN_MS;
}

function executePermissionGrant({ platform, id, current, beforeResults, prompt, openSettings }) {
  if (platform !== 'darwin') {
    return { ok: false, skipped: 'not_required', prompted: false, openedSettings: false };
  }
  const definition = permissionById(platform, id);
  if (!definition) return { ok: false, skipped: 'unknown_permission', prompted: false, openedSettings: false };
  if (current === 'granted') {
    return { ok: true, id, status: 'granted', prompted: false, openedSettings: false };
  }
  if (definition.prompts && current !== 'denied') {
    const prompted = prompt();
    const after = automationCardStatus(prompted.results);
    const dialogShown = inferDialogShown({
      before: beforeResults,
      after: prompted.results,
      elapsedMs: prompted.elapsedMs,
    });
    const opened = after !== 'granted' && (!dialogShown || prompted.incomplete);
    if (opened) openSettings(definition.settingsUrl);
    return {
      ok: true,
      id,
      status: after === 'granted' ? 'granted' : 'not_granted',
      prompted: true,
      openedSettings: opened,
      dialogShown,
    };
  }
  openSettings(definition.settingsUrl);
  return {
    ok: true,
    id,
    status: 'not_granted',
    prompted: false,
    openedSettings: true,
    guidance: id === 'full_disk' ? FDA_GUIDANCE : '',
  };
}

function reduceGrantUi(state, event) {
  const current = state || { grantingId: null, notice: '', noticeId: null };
  if (!event || event.type === 'clear') return { grantingId: null, notice: '', noticeId: null };
  if (event.type === 'start') return { grantingId: event.id || null, notice: '', noticeId: null };
  if (event.type === 'timeout') {
    if (!current.grantingId) return current;
    return { grantingId: null, notice: SETTINGS_GUIDANCE, noticeId: current.grantingId };
  }
  if (event.type === 'result') {
    if (event.status === 'granted') return { grantingId: null, notice: '', noticeId: null };
    const notice = event.guidance || (event.openedSettings && event.id === 'automation' ? SETTINGS_GUIDANCE : '');
    return { grantingId: null, notice, noticeId: notice ? (event.id || null) : null };
  }
  return current;
}

function completePermissionGuide({ platform, canContinue, statuses = {}, writeMarker }) {
  if (platform !== 'darwin') return { saved: false, skipped: 'not_required' };
  if (!canContinue) return { saved: false, skipped: 'required_missing' };
  const record = {
    version: 2,
    completed: true,
    statuses: {
      automation: statuses.automation === 'granted' ? 'granted' : 'not_granted',
      full_disk: statuses.full_disk === 'granted' ? 'granted' : 'not_granted',
    },
  };
  writeMarker(record);
  return { saved: true, record };
}

module.exports = {
  DIALOG_SHOWN_MS,
  GRANT_VISIBLE_MS,
  GRANT_PROMPT_TIMEOUT_MS,
  GRANT_UI_TIMEOUT_MS,
  SETTINGS_GUIDANCE,
  FDA_GUIDANCE,
  OPEN_WECHAT_GUIDANCE,
  DIALOG_GUIDANCE,
  SETTINGS_URLS,
  AUTOMATION_CHECKS,
  MAC_PERMISSIONS,
  interpretAutomationCode,
  automationCardStatus,
  automationGapNote,
  automationSettingsGuidance,
  classifyReadError,
  fullDiskStatus,
  fullDiskProbeResult,
  statusesFromProbes,
  permissionById,
  presentCard,
  permissionGuide,
  loadingPermissionGuide,
  applyOnboardingKey,
  inferDialogShown,
  reduceGrantUi,
  executePermissionGrant,
  completePermissionGuide,
};

'use strict';

// Scan card for the last step of the Mac permission wizard.
// The view is derived only from AutoCaptureJob.snapshot() fields. No secrets.

const COPY = {
  pageTitle: '连接你的微信',
  intro: '扫码连接，让 AI 读懂你的微信。',
  title: '扫码连接',
  detail: '将打开临时微信供你扫码，完成后自动清理。',
  risk: '扫码会让当前微信下线一次，完成后重新打开原微信。',
};

const LABELS = {
  ready: '开始扫码登录',
  waiting: '等待扫码',
  captured: '已捕获登录',
  verifying: '正在校验',
  done: '完成',
  failed: '未检测到登录，请重试',
  blocked: '未找到微信资料',
  error: '准备失败，请重试',
};

function presentConnectCard(snapshot) {
  const snap = snapshot && typeof snapshot === 'object' ? snapshot : {};
  if (snap.error === 'not_required' || snap.error === 'windows_key_path_unchanged') {
    return card('done', { canContinue: true, canStart: false });
  }
  if (snap.error === 'app_store_wechat') {
    return {
      ...card('error', { canStart: true, canContinue: false }),
      title: '请使用微信官网版',
      detail: '检测到 App Store 版，暂不支持连接。',
      risk: '请先备份聊天记录，再安装官网版。',
      actionLabel: '安装后重新检测',
    };
  }
  if (snap.error === 'wechat_restore_failed') {
    return {
      ...card('error', { canStart: false, canContinue: false }),
      title: '连接已保存，请重新打开微信',
      detail: '请完全退出临时微信，再从“应用程序”打开官方微信并登录，然后重启 Noteford。',
      risk: '无需重新扫码取钥。',
      actionLabel: '等待恢复微信',
    };
  }
  const scan = String(snap.scan_status || '');
  const phase = String(snap.phase || '');
  const running = !!snap.running;
  if (!running && (snap.scan_status === 'blocked' || snap.stale_reason === 'databases_not_found')) {
    return {
      ...card('blocked', { canStart: true, canContinue: false }),
      title: '先登录微信',
      detail: '请打开官网版微信并登录，再回来重新检测。',
      risk: '',
      actionLabel: '重新检测',
    };
  }
  if (!running && snap.error === 'account_initialization_failed') {
    return {
      ...card('error', { canStart: true, canContinue: false }),
      title: '微信资料尚未准备好',
      detail: '扫码已完成，但资料读取未完成。请确认磁盘空间充足后重试。',
      actionLabel: '重新连接',
    };
  }
  const userFailed = snap.error === 'no_passphrase_captured' || snap.error === 'coverage_incomplete';
  let status = 'ready';
  if (scan === 'blocked' || snap.stale_reason === 'databases_not_found') status = 'blocked';
  else if (!running && (userFailed || scan === 'failed' || phase === 'failed')) status = userFailed || !snap.error ? 'failed' : 'error';
  else if (snap.keys_already_valid || (snap.ok && (phase === 'done' || phase === 'already_satisfied' || scan === 'done'))) status = 'done';
  else if (scan === 'verifying' || phase === 'derive_verify' || phase === 'sync') status = 'verifying';
  else if (scan === 'captured' || phase === 'captured') status = 'captured';
  else if (running || scan === 'waiting' || phase === 'await_scan' || phase === 'prepare_copy' || phase === 'build_hook' || phase === 'preflight') status = 'waiting';
  else status = 'ready';
  return card(status, {
    canContinue: status === 'done',
    canStart: status === 'ready' || status === 'failed' || status === 'error',
  });
}

function card(status, flags) {
  return {
    pageTitle: COPY.pageTitle,
    intro: COPY.intro,
    title: COPY.title,
    detail: COPY.detail,
    risk: COPY.risk,
    actionLabel: LABELS[status] || LABELS.ready,
    status,
    canContinue: !!flags.canContinue,
    canStart: !!flags.canStart,
    busy: status === 'waiting' || status === 'captured' || status === 'verifying',
  };
}

module.exports = {
  COPY,
  LABELS,
  presentConnectCard,
};

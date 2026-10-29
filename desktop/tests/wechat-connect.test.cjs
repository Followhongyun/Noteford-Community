const { test } = require('node:test');
const assert = require('node:assert/strict');
const { presentConnectCard, COPY } = require('../electron/wechat-connect.cjs');

test('the scan card starts idle and explains the one-time logout', () => {
  const card = presentConnectCard(null);
  assert.equal(card.pageTitle, '连接你的微信');
  assert.equal(card.title, '扫码连接');
  assert.match(card.detail, /自动清理/);
  assert.match(card.risk, /下线一次/);
  assert.match(card.risk, /重新打开原微信/);
  assert.equal(card.actionLabel, '开始扫码登录');
  assert.equal(card.status, 'ready');
  assert.equal(card.canStart, true);
  assert.equal(card.canContinue, false);
  assert.equal(COPY.detail.includes('LSBackgroundOnly'), false);
});

test('polling snapshot moves the card through the scan states without secrets', () => {
  const waiting = presentConnectCard({ running: true, phase: 'await_scan', needs_scan: true, scan_status: 'waiting', ok: false });
  assert.equal(waiting.status, 'waiting');
  assert.equal(waiting.actionLabel, '等待扫码');
  assert.equal(waiting.canStart, false);

  const captured = presentConnectCard({ running: true, phase: 'captured', scan_status: 'captured', needs_scan: false });
  assert.equal(captured.actionLabel, '已捕获登录');

  const verifying = presentConnectCard({ running: true, phase: 'derive_verify', scan_status: 'verifying' });
  assert.equal(verifying.actionLabel, '正在校验');

  const done = presentConnectCard({ running: false, ok: true, phase: 'done', scan_status: 'done', keys_already_valid: true });
  assert.equal(done.status, 'done');
  assert.equal(done.actionLabel, '完成');
  assert.equal(done.canContinue, true);
  assert.equal(done.canStart, false);

  const failed = presentConnectCard({
    running: false,
    ok: false,
    phase: 'failed',
    error: 'no_passphrase_captured',
    needs_scan: true,
    scan_status: 'failed',
  });
  assert.equal(failed.actionLabel, '未检测到登录，请重试');
  assert.equal(failed.canStart, true);
  assert.equal(failed.canContinue, false);
  const infra = presentConnectCard({ running: false, ok: false, phase: 'failed', error: 'copy_failed', scan_status: 'failed' });
  assert.equal(infra.actionLabel, '准备失败，请重试');
  assert.equal(infra.canStart, true);

  const blocked = presentConnectCard({ scan_status: 'blocked', stale_reason: 'databases_not_found', needs_scan: false });
  assert.equal(blocked.status, 'blocked');
  assert.equal(blocked.canStart, true);
  assert.match(blocked.detail, /打开官网版微信并登录/);
  assert.equal(blocked.canContinue, false);

  const blob = JSON.stringify({ waiting, captured, verifying, done, failed, blocked });
  assert.equal(blob.includes('key_hex'), false);
  assert.equal(blob.includes('passphrase'), false);
});

test('verified keys cannot hide a failed first index', () => {
  const failed = presentConnectCard({keys_already_valid: true, ok: false,
    phase: 'failed', scan_status: 'failed', error: 'account_initialization_failed'});
  assert.equal(failed.canContinue, false);
  assert.equal(failed.canStart, true);
  assert.match(failed.detail, /扫码已完成/);
});

test('Windows and an already-valid key do not ask for another scan', () => {
  const windows = presentConnectCard({ error: 'windows_key_path_unchanged', ok: false });
  assert.equal(windows.status, 'done');
  assert.equal(windows.canContinue, true);
  const fresh = presentConnectCard({ ok: true, phase: 'already_satisfied', keys_already_valid: true, needs_scan: false });
  assert.equal(fresh.canContinue, true);
  assert.equal(fresh.canStart, false);
});

test('App Store build gives actionable guidance instead of a generic retry', () => {
  const card = presentConnectCard({phase: 'failed', error: 'app_store_wechat', scan_status: 'failed'});
  assert.equal(card.title, '请使用微信官网版');
  assert.match(card.detail, /App Store/);
  assert.equal(card.actionLabel, '安装后重新检测');
  assert.equal(card.canStart, true);
  assert.equal(card.canContinue, false);
});

test('restore failure explains manual recovery without asking for another key capture', () => {
  const card = presentConnectCard({error:'wechat_restore_failed', keys_already_valid:true, running:false});
  assert.equal(card.canContinue,false);
  assert.equal(card.canStart,false);
  assert.match(card.detail,/完全退出临时微信/);
  assert.match(card.risk,/无需重新扫码/);
});

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const seam = require('../electron/platform-seam.cjs');
const mac = require('../electron/mac-permissions.cjs');

const desktop = path.resolve(__dirname, '..');

function grantedStatuses() {
  return { automation: 'granted', full_disk: 'granted' };
}

test('Windows has no permission cards and does not prompt', () => {
  const guide = seam.permissionGuide({ platform: 'win32', statuses: grantedStatuses() });
  assert.equal(guide.active, false);
  assert.deepEqual(guide.cards, []);
  assert.equal(guide.canContinue, true);
  assert.deepEqual(guide.steps.map(step => step.id), ['account']);
  const calls = [];
  const grant = seam.executePermissionGrant({
    platform: 'win32',
    id: 'automation',
    current: 'not_determined',
    beforeResults: [],
    prompt: () => { calls.push('prompt'); return { results: [], elapsedMs: 0 }; },
    openSettings: () => { calls.push('settings'); },
  });
  assert.equal(grant.skipped, 'not_required');
  assert.deepEqual(calls, []);
  const key = seam.applyOnboardingKey({ platform: 'win32', step: 'account', key: 'Enter', canContinue: true });
  assert.equal(key.handled, false);
  assert.equal(key.step, 'account');
  let wrote = false;
  const saved = seam.completePermissionGuide({
    platform: 'win32',
    canContinue: true,
    statuses: grantedStatuses(),
    writeMarker: () => { wrote = true; },
  });
  assert.equal(saved.skipped, 'not_required');
  assert.equal(wrote, false);
  const profile = seam.hostProfile({ platform: 'win32' });
  assert.equal(profile.onboarding.intro, seam.WINDOWS_INTRO);
  assert.equal(profile.onboarding.permissions.pending, false);
  assert.equal(profile.onboarding.permissions.autoRequest, false);
});

test('Mac guide lists the two required permissions and blocks continue until both are granted', () => {
  const empty = seam.permissionGuide({ platform: 'darwin', statuses: {} });
  assert.equal(empty.active, true);
  assert.equal(empty.title, '需要权限');
  assert.deepEqual(empty.cards.map(card => card.title), ['自动化', '完全磁盘访问']);
  assert.deepEqual(empty.cards.map(card => card.required), [true, true]);
  assert.deepEqual(empty.cards.map(card => card.optionalLabel), ['', '']);
  assert.deepEqual(empty.cards.map(card => card.actionLabel), ['授权', '授权']);
  assert.equal(empty.canContinue, false);
  assert.equal(empty.cards[1].restartNote.includes('重启'), true);
  assert.equal(empty.cards[0].restartNote, '');
  assert.match(empty.cards[0].detail, /退出微信/);
  assert.match(empty.cards[1].detail, /只读/);
  assert.deepEqual(empty.steps.map(step => step.id), ['permissions', 'connect']);

  const automationOnly = seam.permissionGuide({ platform: 'darwin', statuses: { automation: 'granted' } });
  assert.equal(automationOnly.cards[0].status, 'granted');
  assert.equal(automationOnly.cards[0].actionLabel, '已授权');
  assert.equal(automationOnly.canContinue, false);

  const ready = seam.permissionGuide({ platform: 'darwin', statuses: grantedStatuses() });
  assert.equal(ready.canContinue, true);
  assert.deepEqual(ready.cards.map(card => card.status), ['granted', 'granted']);

  const optional = seam.presentCard({ id: 'sample', title: '示例', detail: '说明', required: false }, 'unknown');
  assert.equal(optional.optionalLabel, '可选');
  assert.equal(optional.required, false);
  assert.equal(optional.actionLabel, '授权');
});

test('grant opens a system prompt only when macOS will still ask', () => {
  const urls = [];
  const prompts = [];
  const ask = seam.executePermissionGrant({
    platform: 'darwin',
    id: 'automation',
    current: 'not_determined',
    beforeResults: [{ target: 'system_events', event: 'getd', code: -1744 }],
    prompt: () => {
      prompts.push('ask');
      return {
        results: seam.AUTOMATION_CHECKS.map(check => ({ target: check.target, event: check.event, code: 0 })),
        elapsedMs: 20,
        incomplete: false,
      };
    },
    openSettings: url => urls.push(url),
  });
  assert.deepEqual(prompts, ['ask']);
  assert.deepEqual(urls, []);
  assert.equal(ask.status, 'granted');
  assert.equal(ask.openedSettings, false);

  const denied = seam.executePermissionGrant({
    platform: 'darwin',
    id: 'automation',
    current: 'denied',
    beforeResults: [],
    prompt: () => { prompts.push('again'); return { results: [], elapsedMs: 0 }; },
    openSettings: url => urls.push(url),
  });
  assert.equal(denied.prompted, false);
  assert.equal(denied.openedSettings, true);
  assert.equal(urls[0], seam.SETTINGS_URLS.automation);

  const disk = seam.executePermissionGrant({
    platform: 'darwin',
    id: 'full_disk',
    current: 'denied',
    beforeResults: [],
    prompt: () => { prompts.push('disk'); return { results: [], elapsedMs: 0 }; },
    openSettings: url => urls.push(url),
  });
  assert.equal(disk.prompted, false);
  assert.equal(disk.openedSettings, true);
  assert.equal(disk.guidance, seam.FDA_GUIDANCE);
  assert.match(disk.guidance, /点「\+」/);
  assert.equal(urls[1], seam.FULL_DISK_SETTINGS);

  const already = seam.executePermissionGrant({
    platform: 'darwin',
    id: 'full_disk',
    current: 'granted',
    beforeResults: [],
    prompt: () => { throw new Error('should not prompt'); },
    openSettings: () => { throw new Error('should not open settings'); },
  });
  assert.equal(already.status, 'granted');
  assert.equal(already.prompted, false);
});

test('a fast refusal with no dialog opens Settings, a shown dialog does not', () => {
  const before = seam.AUTOMATION_CHECKS.map(check => ({ target: check.target, event: check.event, code: -1744 }));
  const silent = seam.inferDialogShown({ before, after: before, elapsedMs: 30 });
  assert.equal(silent, false);
  const answered = seam.inferDialogShown({
    before,
    after: seam.AUTOMATION_CHECKS.map(check => ({ target: check.target, event: check.event, code: -1743 })),
    elapsedMs: 40,
  });
  assert.equal(answered, true);

  const urls = [];
  const closed = seam.executePermissionGrant({
    platform: 'darwin',
    id: 'automation',
    current: 'not_determined',
    beforeResults: before,
    prompt: () => ({ results: before, elapsedMs: 40, incomplete: false }),
    openSettings: url => urls.push(url),
  });
  assert.equal(closed.dialogShown, false);
  assert.equal(closed.openedSettings, true);
  assert.equal(urls.length, 1);

  const shown = seam.executePermissionGrant({
    platform: 'darwin',
    id: 'automation',
    current: 'not_determined',
    beforeResults: before,
    prompt: () => ({
      results: seam.AUTOMATION_CHECKS.map(check => ({ target: check.target, event: check.event, code: -1743 })),
      elapsedMs: 40,
      incomplete: false,
    }),
    openSettings: () => urls.push('extra'),
  });
  assert.equal(shown.dialogShown, true);
  assert.equal(shown.openedSettings, false);
  assert.equal(urls.length, 1);
});

test('keyboard continues only when required permissions are granted', () => {
  const blocked = seam.applyOnboardingKey({ platform: 'darwin', step: 'permissions', key: 'Enter', canContinue: false });
  assert.equal(blocked.changed, false);
  assert.equal(blocked.handled, true);
  const right = seam.applyOnboardingKey({ platform: 'darwin', step: 'permissions', key: 'ArrowRight', canContinue: true });
  assert.equal(right.step, 'connect');
  assert.equal(right.changed, true);
  const left = seam.applyOnboardingKey({ platform: 'darwin', step: 'permissions', key: 'ArrowLeft', canContinue: true });
  assert.equal(left.changed, false);
  assert.equal(left.step, 'permissions');
  const connectBlocked = seam.applyOnboardingKey({
    platform: 'darwin', step: 'connect', key: 'Enter', canContinue: true, connectCanContinue: false,
  });
  assert.equal(connectBlocked.step, 'connect');
  assert.equal(connectBlocked.changed, false);
  const connectDone = seam.applyOnboardingKey({
    platform: 'darwin', step: 'connect', key: 'Enter', canContinue: true, connectCanContinue: true,
  });
  assert.equal(connectDone.step, 'account');
  assert.equal(connectDone.changed, true);
  const back = seam.applyOnboardingKey({ platform: 'darwin', step: 'connect', key: 'ArrowLeft', canContinue: true });
  assert.equal(back.step, 'permissions');
  assert.equal(back.changed, true);
  const account = seam.applyOnboardingKey({ platform: 'darwin', step: 'account', key: 'Enter', canContinue: true });
  assert.equal(account.handled, false);
});

test('completion marker is written only after required grants, without secrets', () => {
  let record = null;
  const blocked = seam.completePermissionGuide({
    platform: 'darwin',
    canContinue: false,
    statuses: { automation: 'granted', full_disk: 'denied' },
    writeMarker: value => { record = value; },
  });
  assert.equal(blocked.skipped, 'required_missing');
  assert.equal(record, null);
  const saved = seam.completePermissionGuide({
    platform: 'darwin',
    canContinue: true,
    statuses: { automation: 'granted', full_disk: 'granted', home: '/Users/secret' },
    writeMarker: value => { record = value; },
  });
  assert.equal(saved.saved, true);
  assert.equal(record.version, 2);
  assert.equal(record.completed, true);
  assert.deepEqual(record.statuses, { automation: 'granted', full_disk: 'granted' });
  assert.equal(JSON.stringify(record).includes('secret'), false);
  assert.equal(seam.permissionMarkerPath('/tmp/noteford').endsWith(`${path.sep}permissions-requested.json`), true);
});

test('status probes tell granted, denied, and missing apart without treating a missing folder as granted', () => {
  assert.equal(seam.interpretAutomationCode(0), 'granted');
  assert.equal(seam.interpretAutomationCode(-1743), 'denied');
  assert.equal(seam.interpretAutomationCode(-1744), 'not_determined');
  assert.equal(seam.automationCardStatus([
    { target: 'system_events', event: 'getd', code: 0 },
    { target: 'wechat', event: 'getd', code: 0 },
    { target: 'wechat', event: 'quit', code: -1744 },
  ]), 'missing_wechat');
  assert.equal(seam.automationCardStatus([
    { target: 'system_events', event: 'getd', code: 0 },
    { target: 'wechat', event: 'getd', code: -1744 },
    { target: 'wechat', event: 'quit', code: -1744 },
  ]), 'missing_wechat');
  assert.equal(seam.automationCardStatus([
    { target: 'system_events', event: 'getd', code: 0 },
    { target: 'wechat', event: 'getd', code: 0 },
    { target: 'wechat', event: 'quit', code: 0 },
  ]), 'granted');
  assert.equal(seam.fullDiskStatus([{ id: 'container', result: 'missing' }, { id: 'tcc', result: 'missing' }]), 'not_determined');
  assert.equal(seam.fullDiskStatus([{ id: 'container', result: 'denied' }, { id: 'tcc', result: 'missing' }]), 'not_determined');
  assert.equal(seam.fullDiskStatus([{ id: 'container', result: 'ok' }, { id: 'tcc', result: 'denied' }]), 'denied');
  assert.equal(seam.fullDiskStatus([{ id: 'tcc', result: 'ok', proves: true }]), 'granted');
  assert.equal(seam.fullDiskProbeResult({ kind: 'file', bytes: 0 }), 'denied');
  assert.equal(seam.fullDiskProbeResult({ kind: 'file', bytes: 16 }), 'ok');
  assert.equal(seam.classifyReadError({ code: 'ENOENT' }), 'missing');
  assert.equal(seam.classifyReadError({ code: 'EPERM' }), 'denied');
  assert.equal(seam.statusesFromProbes({ automation: { ok: false, results: [] }, reads: [] }).automation, 'unknown');
  const home = '/Users/example';
  const root = path.join(home, 'Library', 'Containers', 'com.tencent.xinWeChat');
  const paths = seam.fullDiskProbePaths(home);
  assert.equal(paths.some(item => item.path === root), false);
  assert.equal(paths.length, 2);
  assert.equal(paths[0].kind, 'file');
  assert.match(paths.find(item => item.id === 'tcc').path, /TCC\.db$/);
  assert.equal(paths.find(item => item.id === 'tcc_system').path, '/Library/Application Support/com.apple.TCC/TCC.db');
  const seen = [];
  const blocked = seam.collectFullDiskReads(home, {
    readdirSync(target) {
      seen.push(target);
      if (target === root) return ['Data'];
      const error = new Error('operation not permitted');
      error.code = 'EPERM';
      throw error;
    },
    openSync(target) {
      seen.push(target);
      const error = new Error('operation not permitted');
      error.code = 'EPERM';
      throw error;
    },
    readSync() { return 16; },
    closeSync() {},
  });
  assert.equal(seen.includes(root), false);
  assert.equal(seam.fullDiskStatus(blocked), 'denied');
  const readable = seam.collectFullDiskReads(home, {
    readdirSync() {
      const error = new Error('operation not permitted');
      error.code = 'EPERM';
      throw error;
    },
    openSync() { return 4; },
    readSync() { return 16; },
    closeSync() {},
  });
  assert.equal(seam.fullDiskStatus(readable), 'granted');
});

test('system events without WeChat stays unauthorized and says what is missing', () => {
  const partial = seam.permissionGuide({
    platform: 'darwin',
    statuses: { automation: 'missing_wechat', full_disk: 'not_granted' },
  });
  assert.equal(partial.cards[0].status, 'not_granted');
  assert.equal(partial.cards[0].actionLabel, '授权');
  assert.match(partial.cards[0].gapNote, /微信/);
  assert.match(partial.cards[0].gapNote, /自动化/);
  assert.equal(partial.canContinue, false);
  const ready = seam.permissionGuide({
    platform: 'darwin',
    statuses: { automation: 'granted', full_disk: 'granted' },
  });
  assert.equal(ready.cards[0].gapNote, '');
  assert.equal(ready.cards[0].actionLabel, '已授权');
  assert.equal(ready.canContinue, true);
  assert.match(seam.automationGapNote('missing_system_events'), /系统事件/);
  assert.match(seam.automationSettingsGuidance([]), /系统事件/);
  assert.match(seam.automationSettingsGuidance([]), /微信/);
});

// Every native boundary is injected: these tests never load Electron, launch
// WeChat/System Settings, or send a real Apple Event (including on a Mac runner).
const checker = require('../electron/automation-check.cjs');
const fourCC = text => Buffer.from(text, 'ascii').readUInt32BE(0);
const rowsFor = (system, wechat) => [
  { target: 'system_events', event: 'getd', code: system },
  { target: 'wechat', event: 'getd', code: wechat },
];

function fakeCore({ send = async () => 0, determine = async () => 0 } = {}) {
  const calls = [];
  const disposed = [];
  const api = {
    resolvePid(bundle) { return bundle === 'com.apple.systemevents' ? 101 : 102; },
    create(type, data, length, desc) {
      desc.descriptorType = type;
      if (type === fourCC('kpid')) desc.bundle = data[0] === 101 ? 'com.apple.systemevents' : 'com.tencent.xinWeChat';
      calls.push(['create', type, data, length]);
      return 0;
    },
    createEvent(eventClass, eventId, target, returnId, transaction, event) {
      event.bundle = target.bundle;
      calls.push(['event', eventClass, eventId, target.bundle, returnId, transaction]);
      return 0;
    },
    object(want, container, form, key, disposeInputs) {
      assert.equal(want, fourCC('prop'));
      assert.equal(container.descriptorType, fourCC('null'));
      assert.equal(form, fourCC('prop'));
      assert.equal(disposeInputs, 0);
      return 0;
    },
    put(_event, key) { assert.equal(key, fourCC('----')); return 0; },
    async send(event, reply, flags, timeout) {
      calls.push(['send', event.bundle, flags, timeout]);
      return send(event, reply);
    },
    async determine(target, eventClass, eventId, ask) {
      calls.push(['determine', target.bundle, eventClass, eventId, ask]);
      return determine(target, ask);
    },
    dispose(desc) { disposed.push(desc); return 0; },
  };
  return { api, calls, disposed };
}

test('passive checks use the current process without launching targets or prompting', async () => {
  const fake = fakeCore({ determine: async target => target.bundle === 'com.apple.systemevents' ? 0 : -1744 });
  const rows = await checker.runChecks({
    core: fake.api,
    prepareTarget: () => { throw new Error('status must not launch an app'); },
  });
  assert.deepEqual(rows, rowsFor(0, -1744));
  assert.equal(seam.automationCardStatus(mac.completeAutomationResults(rows)), 'missing_wechat');
  assert.equal(fake.calls.filter(call => call[0] === 'send').length, 0);
  assert.equal(fake.calls.filter(call => call[0] === 'determine').every(call => call[4] === 0), true);
  assert.equal(fake.calls.filter(call => call[0] === 'determine').every(call => call[2] === fourCC('****') && call[3] === fourCC('****')), true);
  assert.equal(fake.disposed.length, 2);
  for (const file of ['automation-check.cjs', 'mac-permissions.cjs']) {
    const source = fs.readFileSync(path.join(desktop, 'electron', file), 'utf8');
    assert.doesNotMatch(source, /osascript|probe\.py|python3|ELECTRON_RUN_AS_NODE|sqlite3|execFileSync/);
  }
});

test('a stalled target preserves the other result and never stacks passive native calls', async () => {
  let finish;
  let attempts = 0;
  const fake = fakeCore({ determine: target => {
    if (target.bundle === 'com.apple.systemevents') return 0;
    attempts++;
    return new Promise(resolve => { finish = resolve; });
  } });
  for (let i = 0; i < 3; i++) {
    assert.deepEqual(await checker.runChecks({ core: fake.api, readTimeoutMs: 5 }), rowsFor(0, -1712));
  }
  assert.equal(attempts, 1);
  assert.equal(seam.automationCardStatus(rowsFor(0, -1712)), 'missing_wechat');
  finish(0);
  await new Promise(resolve => setImmediate(resolve));
  fake.api.determine = async () => -1743;
  assert.deepEqual(await checker.runChecks({ core: fake.api }), rowsFor(-1743, -1743));
});

test('missing live processes do not enter the native permission call', async () => {
  const fake = fakeCore();
  fake.api.resolvePid = () => 0;
  assert.deepEqual(await checker.runChecks({ core: fake.api }), rowsFor(-600, -600));
  assert.equal(fake.calls.length, 0);
});

test('passive checks restart an idle System Events agent but never launch WeChat', async () => {
  const fake = fakeCore();
  let systemPid = 0;
  const prepared = [];
  fake.api.resolvePid = bundle => bundle === 'com.apple.systemevents' ? systemPid : 0;
  const rows = await checker.runChecks({ core: fake.api, prepareTarget: async bundle => {
    prepared.push(bundle);
    systemPid = 101;
  } });
  assert.deepEqual(prepared, ['com.apple.systemevents']);
  assert.deepEqual(rows, rowsFor(0, -600));
});

test('grant sends real get-name events to both targets and keeps descriptors until completion', async () => {
  let finish;
  const blocked = new Promise(resolve => { finish = resolve; });
  const prepared = [];
  const fake = fakeCore({ send: async () => blocked });
  const pending = checker.runChecks({ ask: true, core: fake.api, prepareTarget: async id => prepared.push(id) });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(prepared, ['com.apple.systemevents', 'com.tencent.xinWeChat']);
  const events = fake.calls.filter(call => call[0] === 'event');
  assert.equal(events.length, 2);
  assert.equal(events.every(call => call[1] === fourCC('core') && call[2] === fourCC('getd')), true);
  assert.deepEqual(fake.calls.filter(call => call[0] === 'send').map(call => call.slice(1)), [
    ['com.apple.systemevents', 0x23, 3600], ['com.tencent.xinWeChat', 0x23, 3600],
  ]);
  const properties = fake.calls.filter(call => call[0] === 'create' && call[1] === fourCC('type'));
  assert.equal(properties.every(call => call[2][0] === fourCC('pnam') && call[3] === 4), true);
  assert.equal(fake.disposed.length, 0);
  assert.deepEqual(fake.calls.filter(call => call[0] === 'determine').map(call => call[4]), [1, 1]);
  finish(0);
  assert.deepEqual(await pending, rowsFor(0, 0));
  assert.equal(fake.disposed.length, 12);
  assert.equal(new Set(fake.disposed).size, 12);
  assert.deepEqual(fake.calls.filter(call => call[0] === 'determine').map(call => call[4]), [1, 1, 0, 0]);
});

test('denial, launch races and failed preparation cannot grant or suppress the other target', async () => {
  const fake = fakeCore({ determine: async target => target.bundle === 'com.apple.systemevents' ? -1743 : 0 });
  const denied = await checker.runChecks({ ask: true, core: fake.api });
  assert.deepEqual(denied, rowsFor(-1743, 0));
  assert.equal(fake.disposed.length, 7);
  assert.equal(fake.calls.filter(call => call[0] === 'send').length, 1);
  let attempts = 0;
  const race = fakeCore({ determine: async (target, ask) => ask && target.bundle === 'com.tencent.xinWeChat' && ++attempts < 3 ? -600 : 0 });
  const waits = [];
  assert.deepEqual(await checker.runChecks({ ask: true, core: race.api, wait: async ms => waits.push(ms) }), rowsFor(0, 0));
  assert.deepEqual(waits, [100, 250]);
  const failed = await checker.runChecks({
    ask: true, core: fakeCore().api,
    prepareTarget: async id => { if (id === 'com.apple.systemevents') throw new Error('launch failed'); },
  });
  assert.deepEqual(failed, rowsFor(-1744, 0));
  const unproved = fakeCore({ determine: async () => -1744 });
  assert.deepEqual(await checker.runChecks({ ask: true, core: unproved.api }), rowsFor(-1744, -1744));
});

test('controller deduplicates pending native requests and permits retry after completion', async () => {
  let finish;
  let calls = 0;
  const controller = mac.createAutomationController({
    koffiPath: '/fake/koffi', prepareTarget: async () => {},
    runChecks: async ({ ask, koffiPath }) => {
      assert.equal(ask, true);
      assert.equal(koffiPath, '/fake/koffi');
      calls += 1;
      return new Promise(resolve => { finish = resolve; });
    },
  });
  const first = controller.request();
  assert.equal(controller.request(), first);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 1);
  finish(rowsFor(0, -1743));
  assert.equal(seam.automationCardStatus((await first.done).results), 'missing_wechat');
  const retry = controller.request();
  assert.notEqual(retry, first);
  await new Promise(resolve => setImmediate(resolve));
  finish(rowsFor(0, 0));
  assert.equal(seam.automationCardStatus((await retry.done).results), 'granted');
});

test('slow status reads leave the card usable without accumulating native calls', async () => {
  let finish;
  let calls = 0;
  const controller = mac.createAutomationController({ runChecks: async ({ ask }) => {
    assert.equal(ask, false);
    calls += 1;
    return new Promise(resolve => { finish = resolve; });
  } });
  const status = await controller.read({ timeoutMs: 20 });
  assert.equal(status.timedOut, true);
  assert.equal(status.ok, false);
  await controller.read({ timeoutMs: 20 });
  assert.equal(calls, 1);
  const final = controller.read({ timeoutMs: 100 });
  finish(rowsFor(0, 0));
  assert.equal(seam.automationCardStatus((await final).results), 'granted');
  const broken = mac.createAutomationController({ runChecks: async () => { throw new Error('bridge missing'); } });
  assert.equal((await broken.request().done).ok, false);
  assert.equal((await broken.read()).ok, false);
  assert.notEqual(seam.automationCardStatus(mac.completeAutomationResults(rowsFor(null, ''))), 'granted');
});

test('full disk grant opens AllFiles and reveals Noteford without making the card green', () => {
  const urls = [];
  const revealed = [];
  const result = mac.presentFullDiskGrant({
    current: 'not_determined', execPath: '/Applications/Noteford 开源版.app/Contents/MacOS/Noteford',
    exists: candidate => candidate === '/Applications/Noteford 开源版.app',
    openSettings: url => urls.push(url), revealApp: file => revealed.push(file),
  });
  assert.deepEqual(urls, [seam.SETTINGS_URLS.full_disk]);
  assert.deepEqual(revealed, ['/Applications/Noteford 开源版.app']);
  assert.equal(result.status, 'not_granted');
  assert.match(result.guidance, /点「\+」/);
  assert.match(result.guidance, /重启/);
  assert.deepEqual(mac.revealAppArguments('/dev/Electron.app/Contents/MacOS/Electron', () => true), ['-R', '/Applications/Noteford 开源版.app']);
  assert.equal(mac.revealAppArguments('/dev/Electron.app/Contents/MacOS/Electron', () => false), null);
  assert.equal(seam.fullDiskStatus([{ id: 'wechat_protected', result: 'ok' }, { id: 'tcc', result: 'denied' }]), 'denied');
  assert.equal(seam.fullDiskStatus([{ id: 'wechat_protected', result: 'ok' }]), 'not_determined');
});

test('grant surfaces Settings when the prompt does not finish, and stays quiet when it grants', async () => {
  assert.equal(seam.GRANT_VISIBLE_MS, 1000);
  assert.equal(seam.GRANT_PROMPT_TIMEOUT_MS, 8000);
  assert.ok(seam.GRANT_UI_TIMEOUT_MS > seam.GRANT_VISIBLE_MS);
  const urls = [];
  const started = Date.now();
  const hung = await mac.presentAutomationGrant({
    current: 'not_determined',
    visibleMs: 40,
    startPrompt: () => ({ done: new Promise(() => {}), cancel() {} }),
    openSettings: url => urls.push(url),
  });
  assert.equal(hung.timedOut, true);
  assert.equal(hung.status, 'not_granted');
  assert.equal(hung.openedSettings, true);
  assert.match(hung.guidance, /对话框/);
  assert.deepEqual(urls, [seam.SETTINGS_URLS.automation]);
  assert.ok(Date.now() - started < 500);

  const quiet = [];
  const granted = await mac.presentAutomationGrant({
    current: 'unknown',
    visibleMs: 1000,
    startPrompt: () => ({ done: Promise.resolve({ ok: true }), cancel() {} }),
    readStatus: async () => ({ status: 'granted' }),
    openSettings: url => quiet.push(url),
  });
  assert.equal(granted.status, 'granted');
  assert.deepEqual(quiet, []);

  const refused = [];
  const fast = await mac.presentAutomationGrant({
    current: 'not_determined',
    visibleMs: 1000,
    startPrompt: () => ({ done: Promise.resolve({ ok: false }), cancel() {} }),
    openSettings: url => refused.push(url),
  });
  assert.equal(fast.openedSettings, true);
  assert.equal(fast.timedOut, false);
  assert.match(fast.guidance, /微信/);
  assert.equal(refused[0], seam.SETTINGS_URLS.automation);

  let prompted = false;
  const denied = await mac.presentAutomationGrant({
    current: 'denied',
    visibleMs: 1000,
    startPrompt: () => {
      prompted = true;
      return {
        ok: true,
        results: [
          { target: 'system_events', event: 'getd', code: 0 },
          { target: 'wechat', event: 'getd', code: -1743 },
        ],
      };
    },
    openSettings: url => refused.push(url),
  });
  assert.equal(prompted, true);
  assert.equal(denied.openedSettings, true);
  assert.match(denied.guidance, /微信/);
  assert.doesNotMatch(denied.guidance, /系统事件/);

  let revealed = null;
  const broken = await mac.presentAutomationGrant({
    current: 'not_determined',
    lowerWindow: () => { throw new Error('hide failed after the window was already down'); },
    revealWindow: focus => { revealed = focus; },
    startPrompt: () => { throw new Error('prompt must not be required after a hide failure'); },
    openSettings: url => refused.push(url),
  });
  assert.equal(revealed, false);
  assert.equal(broken.openedSettings, true);
  assert.equal(broken.status, 'not_granted');
  assert.equal(broken.guidance, seam.SETTINGS_GUIDANCE);

  const requesting = seam.reduceGrantUi({ grantingId: null, notice: '' }, { type: 'start', id: 'automation' });
  assert.equal(requesting.grantingId, 'automation');
  const timed = seam.reduceGrantUi(requesting, { type: 'timeout' });
  assert.equal(timed.grantingId, null);
  assert.equal(timed.notice, seam.SETTINGS_GUIDANCE);
  assert.equal(timed.noticeId, 'automation');
  const allowed = seam.reduceGrantUi(requesting, { type: 'result', id: 'automation', status: 'granted' });
  assert.equal(allowed.grantingId, null);
  assert.equal(allowed.notice, '');
  const disk = seam.reduceGrantUi(requesting, { type: 'result', id: 'full_disk', status: 'not_granted', openedSettings: true });
  assert.equal(disk.notice, '');
  const explained = seam.reduceGrantUi(requesting, {
    type: 'result',
    id: 'full_disk',
    status: 'not_granted',
    openedSettings: true,
    guidance: seam.FDA_GUIDANCE,
  });
  assert.equal(explained.notice, seam.FDA_GUIDANCE);
  assert.equal(explained.noticeId, 'full_disk');

  const closed = [];
  const focuses = [];
  const missingWechat = await mac.presentAutomationGrant({
    current: 'unknown',
    startPrompt: () => mac.createAutomationController({
      runChecks: ({ ask }) => {
        assert.equal(ask, true);
        return [
          { target: 'system_events', event: 'getd', code: 0 },
          { target: 'wechat', event: 'getd', code: -600 },
        ];
      },
    }).request(),
    openSettings: url => closed.push(url),
    revealWindow: focus => focuses.push(focus),
  });
  assert.equal(missingWechat.guidance, seam.OPEN_WECHAT_GUIDANCE);
  assert.equal(missingWechat.openedSettings, true);
  assert.deepEqual(closed, [seam.SETTINGS_URLS.automation]);
  assert.deepEqual(focuses, [false]);
  assert.deepEqual(
    mac.revealAppArguments(path.join('/Applications/Noteford 开源版.app/Contents/MacOS/Noteford'), () => false),
    ['-R', path.join('/Applications/Noteford 开源版.app')],
  );
  assert.equal(mac.revealAppArguments('/usr/bin/node', () => false), null);
  assert.equal(
    mac.packagedModulePath(path.join('/Applications/Noteford 开源版.app/Contents/Resources/app.asar/node_modules/koffi/index.js'), candidate => candidate.includes('app.asar.unpacked')),
    path.join('/Applications/Noteford 开源版.app/Contents/Resources/app.asar.unpacked/node_modules/koffi/index.js'),
  );
  assert.equal(mac.packagedModulePath('/workspace/node_modules/koffi/index.js', () => false), '/workspace/node_modules/koffi/index.js');
});

test('the desktop shell keeps the guide off Windows and no longer auto-prompts', () => {
  const app = fs.readFileSync(path.join(desktop, 'src', 'App.tsx'), 'utf8');
  const guide = fs.readFileSync(path.join(desktop, 'src', 'components', 'PermissionGuide.tsx'), 'utf8');
  const preload = fs.readFileSync(path.join(desktop, 'electron', 'preload.cjs'), 'utf8');
  const main = fs.readFileSync(path.join(desktop, 'electron', 'main.cjs'), 'utf8');
  assert.match(app, /useState<'permissions' \| 'connect' \| 'account'>\('account'\)/);
  assert.match(app, /needsGuide \? 'permissions' : needsConnect \? 'connect' : 'account'/);
  assert.match(app, /window\.noteford\.platform === 'darwin' && foundLibraries\.length === 0/);
  assert.match(app, /showPermissionCover = macLayout && onboardingStep !== 'account'/);
  assert.match(app, /connectMacAccount\(\{\}\)/);
  assert.match(app, /选择要读取的微信账号/);
  assert.doesNotMatch(app, /requestHostPermissions|允许这些权限|permissions\.autoRequest/);
  assert.match(guide, /optionalLabel/);
  assert.match(guide, /permission-granted/);
  assert.match(guide, /actionLabel/);
  assert.match(guide, /applyOnboardingKey/);
  assert.match(guide, /permission-card-risk/);
  assert.match(guide, /onStartScan/);
  assert.match(guide, /gapNote/);
  assert.match(guide, /grantNoticeId/);
  assert.match(guide, /正在请求…/);
  assert.match(app, /reduceGrantUi/);
  assert.match(app, /GRANT_UI_TIMEOUT_MS/);
  assert.match(main, /presentAutomationGrant/);
  assert.match(main, /requestAutomationConsent/);
  assert.match(main, /presentFullDiskGrant/);
  assert.match(main, /shell\.showItemInFolder/);
  assert.match(main, /readAutomationStatus/);
  assert.match(main, /showInactive/);
  assert.match(main, /\/usr\/bin\/open/);
  assert.doesNotMatch(main, /queryAutomation/);
  assert.match(preload, /permissionGuide:/);
  assert.match(preload, /autoCaptureStatus:/);
  assert.match(preload, /autoCaptureStart:/);
  assert.match(main, /noteford:auto-capture-status/);
  assert.match(main, /noteford:auto-capture-start/);
  assert.match(preload, /grantPermission:/);
  assert.doesNotMatch(preload, /host-permissions/);
  assert.match(main, /noteford:permission-guide/);
  assert.match(main, /if \(process\.platform !== 'darwin'\)/);
  assert.doesNotMatch(main, /noteford:host-permissions|requestFirstRunPermissions/);
});

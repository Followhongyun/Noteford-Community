'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const guide = require('../electron/permission-guide.cjs');
const seam = require('../electron/platform-seam.cjs');
const mac = require('../electron/mac-permissions.cjs');
const runtime = require('../electron/mac-permission-runtime.cjs');

const results = (system, wechat) => [
  { target: 'system_events', event: 'getd', code: system },
  { target: 'wechat', event: 'getd', code: wechat },
];
const deniedDisk = [{ id: 'tcc', result: 'denied', proves: true }];
const readableDisk = [{ id: 'tcc', result: 'ok', proves: true }];

test('both automation targets are required, including after a grant is revoked', () => {
  for (const [system, wechat, status] of [
    [0, 0, 'granted'], [0, -1743, 'missing_wechat'],
    [-1743, 0, 'missing_system_events'], [-1743, -1743, 'denied'],
    [-1744, -1744, 'not_determined'], [0, -600, 'wechat_not_running'],
    [null, null, 'not_determined'], ['', '', 'not_determined'],
  ]) {
    const statuses = guide.statusesFromProbes({ automation: { ok: true, results: results(system, wechat) }, reads: readableDisk });
    assert.equal(statuses.automation, status);
    const page = guide.permissionGuide({ platform: 'darwin', statuses });
    assert.equal(page.canContinue, status === 'granted');
    assert.equal(page.cards[0].actionLabel, status === 'granted' ? '已授权' : '授权');
    if (status === 'missing_wechat') assert.match(page.cards[0].gapNote, /微信/);
    if (status === 'missing_system_events') assert.match(page.cards[0].gapNote, /系统事件/);
  }
  assert.notEqual(guide.automationCardStatus([{ target: 'system_events', code: 0 }]), 'granted');
  assert.notEqual(guide.interpretAutomationCode(false), 'granted');
});

test('FDA policy allowed but current read denied requires restart and still blocks Continue', () => {
  assert.equal(guide.fullDiskStatus(deniedDisk, { policyAllowed: true }), 'restart_required');
  assert.equal(guide.fullDiskStatus(deniedDisk, { policyAllowed: false }), 'denied');
  assert.equal(guide.fullDiskStatus([], { policyAllowed: true }), 'not_determined');
  const statuses = guide.statusesFromProbes({ automation: { ok: true, results: results(0, 0) }, reads: deniedDisk, policyAllowed: true });
  const page = guide.permissionGuide({ platform: 'darwin', statuses });
  assert.equal(page.canContinue, false);
  assert.equal(page.cards[1].status, 'restart_required');
  assert.equal(page.cards[1].actionLabel, '重启');
  assert.match(page.cards[1].restartNote, /需要重启 Noteford/);
  assert.equal(guide.fullDiskStatus(readableDisk, { policyAllowed: false }), 'granted');
  const ready = guide.permissionGuide({ platform: 'darwin', statuses: { ...statuses, full_disk: guide.fullDiskStatus(readableDisk) } });
  assert.equal(ready.canContinue, true);
  assert.equal(ready.cards[1].restartNote, '');
});

test('FDA requires nonempty bytes, closes descriptors, and never enumerates directories', () => {
  const calls = [];
  for (const bytes of [0, 16]) {
    const reads = seam.collectFullDiskReads('/fixture', {
      openSync(file, mode) { assert.match(file, /com\.apple\.TCC[\\/]TCC\.db$/); assert.equal(mode, 'r'); return 9; },
      readSync(fd, buffer, offset, length, position) { assert.equal(fd, 9); assert.equal(length, 16); assert.equal(position, 0); return bytes; },
      closeSync(fd) { calls.push(fd); },
      readdirSync() { assert.fail('directory access must not be used'); },
    });
    assert.equal(guide.fullDiskStatus(reads), bytes ? 'granted' : 'denied');
  }
  assert.deepEqual(calls, [9, 9, 9, 9]);
  assert.equal(guide.fullDiskProbeResult({ kind: 'dir', entries: 100 }), 'error');
  assert.equal(seam.readFullDiskTarget({ kind: 'file', path: '/fixture' }, {
    openSync() { return 9; },
    readSync() { throw Object.assign(new Error(), { code: 'EPERM' }); },
    closeSync(fd) { calls.push(fd); },
  }), 'denied');
  assert.equal(calls.length, 5);
});

test('relocated user TCC database falls back to a real system database read', () => {
  for (const accessible of [false, true]) {
    const opened = [];
    const reads = seam.collectFullDiskReads('/fixture', {
      openSync(file, mode) {
        opened.push(file);
        assert.equal(mode, 'r');
        if (file.startsWith(path.join('/fixture') + path.sep)) throw Object.assign(new Error(), { code: 'ENOENT' });
        assert.equal(file, '/Library/Application Support/com.apple.TCC/TCC.db');
        if (!accessible) throw Object.assign(new Error(), { code: 'EPERM' });
        return 9;
      },
      readSync(fd, buffer, offset, length, position) {
        assert.equal(fd, 9);
        assert.equal(length, 16);
        assert.equal(position, 0);
        return 16;
      },
      closeSync(fd) { assert.equal(fd, 9); },
      readdirSync() { assert.fail('directory access must not be used'); },
    });
    assert.equal(opened.length, 2);
    assert.equal(guide.fullDiskStatus(reads), accessible ? 'granted' : 'denied');
    assert.equal(guide.fullDiskStatus(reads, { policyAllowed: true }), accessible ? 'granted' : 'restart_required');
  }
  assert.equal(guide.fullDiskStatus([{ id: 'tcc_system', result: 'ok', proves: false }]), 'not_determined');
});

test('FDA SPI is only an optional bounded hint and does not accumulate probes', async () => {
  for (const [code, expected] of [[0, true], [1, false], [2, null], [-1, null], [null, null]]) {
    assert.equal(await runtime.createFullDiskPolicyReader({ load: () => async () => code })(), expected);
  }
  assert.equal(await runtime.createFullDiskPolicyReader({ load: () => { throw new Error('unavailable'); } })(), null);
  let finish;
  let calls = 0;
  const read = runtime.createFullDiskPolicyReader({ load: () => () => {
    calls++;
    return new Promise(resolve => { finish = resolve; });
  } });
  assert.equal(await read({ timeoutMs: 5 }), null);
  assert.equal(await read({ timeoutMs: 5 }), null);
  assert.equal(calls, 1);
  const pending = read();
  finish(0);
  assert.equal(await pending, true);
});

test('focus events recheck immediately, coalesce polling and queue a fresh read during a toggle', async () => {
  const app = new EventEmitter();
  const published = [];
  let finish;
  let count = 0;
  const monitor = runtime.createPermissionMonitor({
    read: () => {
      if (++count === 1) return new Promise(resolve => { finish = resolve; });
      return Promise.resolve({ canContinue: true });
    },
    publish: value => published.push(value),
  });
  const unbind = runtime.bindPermissionFocus(app, monitor.refresh);
  app.emit('browser-window-focus');
  const first = monitor.refresh();
  assert.equal(monitor.refresh(), first);
  app.emit('did-become-active');
  finish({ canContinue: false });
  assert.deepEqual(await first, { canContinue: true, revision: 2 });
  assert.deepEqual(published.map(value => value.canContinue), [false, true]);
  assert.equal(count, 2);
  unbind();
  assert.equal(app.listenerCount('browser-window-focus'), 0);
  assert.equal(app.listenerCount('did-become-active'), 0);
});

test('closed WeChat gets guidance before any prompt or settings launch', async () => {
  const value = await mac.presentAutomationGrant({
    wechatRunning: false,
    startPrompt() { assert.fail('must not prompt'); },
    lowerWindow() { assert.fail('must not hide'); },
    openSettings() { assert.fail('must not open settings'); },
  });
  assert.equal(value.prompted, false);
  assert.equal(value.status, 'not_granted');
  assert.match(value.guidance, /先打开微信/);
});

test('ad-hoc signature changes are explained without assuming a duplicate TCC row exists', async () => {
  const signature = runtime.parseSignature('Identifier=com.followhongyun.noteford.community\nSignature=adhoc\nCDHash=abcd1234\n# designated => cdhash H"abcd1234"');
  assert.equal(signature.hashRequirement, true);
  assert.match(runtime.signatureGuidance(signature, { cdhash: 'eeee' }), /签名已变化/);
  assert.match(runtime.signatureGuidance(signature), /若出现多条/);
  assert.equal(runtime.signatureGuidance({ ...signature, adhoc: false }), '');
  assert.match(runtime.signatureGuidance({ identifier: 'com.github.Electron' }), /无法确认/);
  const writes = [];
  const notice = await runtime.readSignatureNotice({ bundle: '/fixture/Noteford 开源版.app', marker: '/fixture/marker.json',
    run: async (command, args) => {
      assert.equal(command, '/usr/bin/codesign');
      assert.equal(args.at(-1), '/fixture/Noteford 开源版.app');
      return { stdout: '', stderr: 'Identifier=com.followhongyun.noteford.community\nSignature=adhoc\nCDHash=abcd1234' };
    },
    io: { readFileSync: () => '{"cdhash":"eeee"}', mkdirSync() {}, writeFileSync: (_file, value) => writes.push(JSON.parse(value)) },
  });
  assert.match(notice, /签名已变化/);
  assert.equal(writes[0].cdhash, 'abcd1234');
});

test('restart schedules relaunch then uses the normal shutdown path, and is Mac only', () => {
  const calls = [];
  const app = { relaunch: () => calls.push('relaunch'), quit: () => calls.push('quit') };
  assert.equal(runtime.restartForPermissions(app, 'win32').ok, false);
  assert.deepEqual(calls, []);
  assert.equal(runtime.restartForPermissions(app, 'darwin').ok, true);
  assert.deepEqual(calls, ['relaunch', 'quit']);
});

test('native SDK and Koffi agree on AEDesc/AEAddressDesc ABI without sending events', {
  skip: process.platform !== 'darwin' || process.env.NOTEFORD_TEST_NATIVE !== '1',
}, () => {
  const compiled = spawnSync('/usr/bin/xcrun', ['clang', '-x', 'c', '-fsyntax-only', '-'], {
    input: '#include <CoreServices/CoreServices.h>\n#include <stddef.h>\n_Static_assert(sizeof(AEDesc)==12,"AEDesc size");\n_Static_assert(offsetof(AEDesc,dataHandle)==4,"AEDesc handle");\n_Static_assert(_Alignof(AEDesc)==2,"AEDesc alignment");\n_Static_assert(sizeof(AEAddressDesc)==12,"address size");\n', encoding: 'utf8',
  });
  assert.equal(compiled.status, 0, compiled.stderr);
  const core = require('../electron/automation-check.cjs').loadCore();
  assert.equal(core.resolvePid('com.followhongyun.noteford.community.nonexistent-test-target'), 0);
  const desc = { descriptorType: 0, dataHandle: null };
  const data = new Int32Array([process.pid]);
  assert.equal(core.create(0x6b706964, data, 4, desc), 0);
  assert.equal(desc.descriptorType, 0x6b706964);
  assert.ok(desc.dataHandle);
  assert.equal(core.dispose(desc), 0);
});

 test('source permission restart delegates to its supervisor and never launches an orphan', () => {
  const app={relaunch(){assert.fail('source must not relaunch directly')},quit(){assert.fail('supervisor owns normal quit')}};
  let requests=0;
  assert.deepEqual(runtime.restartForPermissions(app,'darwin',{requestRestart:()=>{requests++;return true}}),{ok:true});
  assert.equal(requests,1);
  assert.deepEqual(runtime.restartForPermissions(app,'darwin',{requestRestart:()=>false}),{ok:false});
  assert.deepEqual(runtime.restartForPermissions(app,'win32',{requestRestart:()=>assert.fail()}),{ok:false});
});

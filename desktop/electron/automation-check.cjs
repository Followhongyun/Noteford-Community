'use strict';

// Loaded by Electron's main process: every native call keeps Noteford's PID and
// code signature. Koffi's async calls run on native threads, never on the UI loop.
const { promisify } = require('node:util');
const fourCC = text => Buffer.from(text, 'ascii').readUInt32BE(0);
const CHECKS = [
  { target: 'system_events', bundleId: 'com.apple.systemevents' },
  { target: 'wechat', bundleId: 'com.tencent.xinWeChat' },
];
const cores = new Map();
const pendingReads = new WeakMap();
let nextType = 0;

function runningApplicationResolver(koffi) {
  // Resolve the live process in this app, rather than asking Apple Event
  // Manager to resolve a possibly stale LaunchServices bundle registration.
  koffi.load('/System/Library/Frameworks/AppKit.framework/AppKit');
  const objc = koffi.load('/usr/lib/libobjc.A.dylib');
  const cf = koffi.load('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation');
  const getClass = objc.func('void * objc_getClass(const char *)');
  const selector = objc.func('void * sel_registerName(const char *)');
  const object = objc.func('objc_msgSend', 'void *', ['void *', 'void *']);
  const withObject = objc.func('objc_msgSend', 'void *', ['void *', 'void *', 'void *']);
  const atIndex = objc.func('objc_msgSend', 'void *', ['void *', 'void *', 'unsigned long']);
  const count = objc.func('objc_msgSend', 'unsigned long', ['void *', 'void *']);
  const pid = objc.func('objc_msgSend', 'int32_t', ['void *', 'void *']);
  const string = cf.func('void * CFStringCreateWithCString(void *, const char *, uint32_t)');
  const release = cf.func('void CFRelease(void *)');
  const running = getClass('NSRunningApplication');
  const selectors = Object.fromEntries(['new', 'drain', 'runningApplicationsWithBundleIdentifier:', 'count', 'objectAtIndex:', 'processIdentifier'].map(name => [name, selector(name)]));
  return bundleId => {
    const pool = object(getClass('NSAutoreleasePool'), selectors.new);
    const name = string(null, bundleId, 0x08000100);
    try {
      const apps = withObject(running, selectors['runningApplicationsWithBundleIdentifier:'], name);
      if (!apps || count(apps, selectors.count) === 0) return 0;
      return pid(atIndex(apps, selectors['objectAtIndex:'], 0), selectors.processIdentifier);
    } finally {
      if (name) release(name);
      object(pool, selectors.drain);
    }
  };
}

function loadCore(koffiPath = require.resolve('koffi')) {
  if (cores.has(koffiPath)) return cores.get(koffiPath);
  const koffi = require(koffiPath);
  const library = koffi.load('/System/Library/Frameworks/CoreServices.framework/CoreServices');
  // AEDataModel.h uses #pragma pack(push, 2), including on arm64. Default
  // alignment puts dataHandle at offset 8 instead of 4 and corrupts descriptors.
  const type = `NotefordAEDesc${nextType++}`;
  const AEDesc = koffi.struct(type, {
    descriptorType: [2, 'uint32_t'],
    dataHandle: [2, 'void *'],
  });
  if (koffi.sizeof(AEDesc) !== 12 || koffi.alignof(AEDesc) !== 2 || koffi.offsetof(AEDesc, 'dataHandle') !== 4) {
    throw new Error('Invalid Apple Event descriptor ABI');
  }
  const fn = signature => library.func(signature.replaceAll('NotefordAEDesc', type));
  const core = {
    resolvePid: runningApplicationResolver(koffi),
    create: fn('int16_t AECreateDesc(uint32_t, const void *, long, _Out_ NotefordAEDesc *)'),
    determine: promisify(fn('int32_t AEDeterminePermissionToAutomateTarget(const NotefordAEDesc *, uint32_t, uint32_t, uint8_t)').async),
    createEvent: fn('int16_t AECreateAppleEvent(uint32_t, uint32_t, const NotefordAEDesc *, int16_t, int32_t, _Out_ NotefordAEDesc *)'),
    object: fn('int16_t CreateObjSpecifier(uint32_t, NotefordAEDesc *, uint32_t, NotefordAEDesc *, uint8_t, _Out_ NotefordAEDesc *)'),
    put: fn('int16_t AEPutParamDesc(_Inout_ NotefordAEDesc *, uint32_t, const NotefordAEDesc *)'),
    send: promisify(fn('int32_t AESendMessage(const NotefordAEDesc *, _Out_ NotefordAEDesc *, int32_t, long)').async),
    dispose: fn('int16_t AEDisposeDesc(_Inout_ NotefordAEDesc *)'),
  };
  cores.set(koffiPath, core);
  return core;
}

function checked(code) {
  if (code !== 0) throw Object.assign(new Error('Apple Event failed'), { code });
}

async function checkTarget(api, check, ask) {
  const descriptors = [];
  const desc = () => {
    const value = { descriptorType: fourCC('null'), dataHandle: null };
    descriptors.push(value);
    return value;
  };
  let code = -1744;
  try {
    const target = desc();
    const pid = api.resolvePid(check.bundleId);
    if (!Number.isInteger(pid) || pid <= 0) return { target: check.target, event: 'getd', code: -600 };
    const processId = new Int32Array([pid]);
    checked(api.create(fourCC('kpid'), processId, 4, target));
    if (ask) {
      // This may wait for consent on a Koffi worker, with the main process PID.
      code = await api.determine(target, fourCC('****'), fourCC('****'), 1);
      if (code !== 0) return { target: check.target, event: 'getd', code };
      // A real, harmless `get name` event registers this sender and this target
      // with TCC. Do not send quit or rely solely on a permission preflight.
      const event = desc();
      checked(api.createEvent(fourCC('core'), fourCC('getd'), target, -1, 0, event));
      const container = desc();
      const property = desc();
      const name = new Uint32Array([fourCC('pnam')]); // FourCharCode in native byte order
      checked(api.create(fourCC('type'), name, 4, property));
      const object = desc();
      checked(api.object(fourCC('prop'), container, fourCC('prop'), property, 0, object));
      checked(api.put(event, fourCC('----'), object));
      const reply = desc();
      // kAEWaitReply | kAECanInteract. The native timeout is 60 seconds;
      // the permission UI returns independently after at most one second.
      await api.send(event, reply, 0x23, 60 * 60);
      // Delivery success is not permission proof (a target may reject the
      // property). Read the actual TCC decision after delivery as this process.
      code = await api.determine(target, fourCC('****'), fourCC('****'), 0);
    } else {
      code = await api.determine(target, fourCC('****'), fourCC('****'), 0);
    }
  } catch (error) {
    code = Number.isInteger(error.code) ? error.code : -1744;
  } finally {
    for (const value of descriptors.reverse()) {
      try { api.dispose(value); } catch { /* continue releasing other descriptors */ }
    }
  }
  return { target: check.target, event: 'getd', code: Number.isInteger(code) ? code : -1744 };
}

async function runChecks({
  ask = false, koffiPath, core, prepareTarget = async () => {},
  readTimeoutMs = 200,
  wait = ms => new Promise(resolve => setTimeout(resolve, ms)),
} = {}) {
  const api = core || loadCore(koffiPath);
  if (!pendingReads.has(api)) pendingReads.set(api, new Map());
  const pending = pendingReads.get(api);
  // Neither target's prompt waits for the user to answer the other one.
  const rows = await Promise.all(CHECKS.map(async check => {
    try {
      if (ask) await prepareTarget(check.bundleId);
      // System Events exits when idle. Relaunch that background system agent
      // before checking; never launch WeChat as a side effect of polling.
      if (!ask && check.target === 'system_events' && !api.resolvePid(check.bundleId)) {
        await prepareTarget(check.bundleId);
      }
      if (!ask) {
        let active = pending.get(check.target);
        if (!active) {
          active = checkTarget(api, check, false);
          pending.set(check.target, active);
          active.finally(() => { if (pending.get(check.target) === active) pending.delete(check.target); });
        }
        let timer;
        try {
          // A stalled target must not hide the other target's current result
          // or enqueue an unbounded number of native calls. Never reuse a
          // previous granted result when the current read has timed out.
          return await Promise.race([active, new Promise(resolve => {
            timer = setTimeout(() => resolve({ target: check.target, event: 'getd', code: -1712 }), readTimeoutMs);
          })]);
        } finally { clearTimeout(timer); }
      }
      let row = await checkTarget(api, check, ask);
      // LaunchServices can return before the target has registered with AE.
      // Retry only that race; a refusal must never cause repeated prompts.
      for (const delay of [100, 250, 500]) {
        if (!ask || ![-600, -10814].includes(row.code)) break;
        await wait(delay);
        row = await checkTarget(api, check, ask);
      }
      return row;
    } catch {
      // A failed target must not prevent the other target's consent request.
      return { target: check.target, event: 'getd', code: -1744 };
    }
  }));
  return rows;
}

module.exports = { CHECKS, loadCore, runChecks };

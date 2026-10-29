'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createServiceRecovery } = require('../electron/service-recovery.cjs');

function fixture() {
  let clock = 0, child = { pid: 1, ready: true }, healthOk = true, startOk = true;
  let starts = 0, kills = 0, healthCalls = 0;
  const pending = new Map(); let id = 0;
  const recovery = createServiceRecovery({
    now: () => clock,
    schedule: (fn, ms) => { pending.set(++id, { fn, at: clock + ms }); return id; },
    cancel: id => pending.delete(id),
    current: () => child,
    start: async () => { starts++; if (!startOk) throw Error('start'); child = { pid: starts + 1, ready: true }; },
    health: async () => { healthCalls++; if (!healthOk) throw Error('offline'); },
    terminate: () => { kills++; child.ready = false; },
  });
  return {
    recovery,
    async tick() {
      const [key, next] = [...pending].sort((a,b) => a[1].at - b[1].at)[0] || [];
      if (!next) return false;
      pending.delete(key); clock = next.at; next.fn();
      for (let i = 0; i < 5; i++) await Promise.resolve();
      return clock;
    },
    exit() { child = null; recovery.exited(); },
    health(value) { healthOk = value; }, start(value) { startOk = value; },
    starting() { child.ready = false; },
    get counts() { return { starts, kills, healthCalls, pending: pending.size }; },
  };
}

test('unexpected child exit recovers without any renderer/API requests', async () => {
  const f = fixture(); f.recovery.start(); await f.tick(); f.exit();
  await f.tick(); assert.equal(f.counts.starts, 1);
  await f.tick(); assert.equal(f.counts.starts, 1);
});
test('failed restarts back off and quit cancels further attempts', async () => {
  const f = fixture(); f.recovery.start(); f.start(false); f.exit();
  assert.equal(await f.tick(), 1000);
  assert.equal(await f.tick(), 2000);
  assert.equal(await f.tick(), 4000);
  f.recovery.stop(); f.exit(); assert.equal(await f.tick(), false);
  assert.equal(f.counts.starts, 3);
});
test('three health failures terminate once; replacement waits for exit', async () => {
  const f = fixture(); f.health(false); f.recovery.start();
  await f.tick(); await f.tick(); await f.tick();
  assert.equal(f.counts.kills, 1); assert.equal(f.counts.starts, 0);
  await f.tick(); assert.equal(f.counts.starts, 0);
  f.health(true); f.exit(); await f.tick(); assert.equal(f.counts.starts, 1);
});
test('resume checks immediately, and pending startup never creates a second writer', async () => {
  const f = fixture(); f.recovery.start(); f.starting();
  await f.tick(); assert.equal(f.counts.starts, 0); assert.equal(f.counts.healthCalls, 0);
  f.recovery.resume(); await Promise.resolve(); assert.equal(f.counts.starts, 0);
  f.recovery.stop(); assert.equal(await f.tick(), false);
});

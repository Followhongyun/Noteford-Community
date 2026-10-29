'use strict';

// Main-process recovery continues when the renderer is hidden or unavailable.
// Never start a replacement until the previous child has actually exited.
function createServiceRecovery({ start, current, health, terminate, now = Date.now,
  schedule = setTimeout, cancel = clearTimeout }) {
  let enabled = false;
  let timer = null;
  let checking = false;
  let failures = 0;
  let retry = 1000;
  let healthySince = null;

  function plan(delay) {
    if (!enabled || timer !== null) return;
    timer = schedule(() => { timer = null; void check(); }, delay);
    timer?.unref?.();
  }
  async function check() {
    if (!enabled || checking) return;
    checking = true;
    let delay = 30000;
    let checkedChild = null;
    try {
      let child = current();
      if (!child) {
        await start();
        child = current();
      }
      if (!enabled) return;
      // A pending start owns its process; do not launch or kill a second child.
      if (!child?.ready) { delay = 1000; return; }
      checkedChild = child;
      await health(child);
      if (!enabled || current()?.pid !== child.pid) return;
      failures = 0;
      healthySince ??= now();
      if (now() - healthySince >= 60000) retry = 1000;
    } catch {
      healthySince = null;
      const child = current();
      if (child?.ready && child.pid === checkedChild?.pid) {
        failures += 1;
        if (failures >= 3) {
          failures = 0;
          // The exit callback schedules recovery. Do not overlap database writers.
          if (enabled) terminate(child);
        }
      }
      delay = retry;
      retry = Math.min(retry * 2, 60000);
    } finally {
      checking = false;
      plan(delay);
    }
  }
  return {
    start() { enabled = true; plan(1000); },
    exited() {
      healthySince = null;
      failures = 0;
      if (timer !== null) { cancel(timer); timer = null; }
      plan(retry);
    },
    resume() {
      if (timer !== null) { cancel(timer); timer = null; }
      void check();
    },
    stop() {
      enabled = false;
      if (timer !== null) { cancel(timer); timer = null; }
    },
  };
}

module.exports = { createServiceRecovery };

'use strict';

// Only public GitHub artifacts; no account credentials or chat data are sent.
function createAppUpdates({ updater, nativeUpdater, app, enabled = true,
  changed = () => {}, timers = { setTimeout, clearTimeout }, initialDelay = 15000 }) {
  let state = {status: enabled ? 'idle' : 'unavailable', version: app.getVersion(),
    availableVersion: null, progress: 0, error: null};
  let timer, pending, candidate, stopped = false, started = false, failures = 0;
  const listeners = [];
  const snapshot = () => ({...state});
  const emit = patch => { if (!stopped) { state = {...state, ...patch}; changed(snapshot()); } };
  const listen = (source, event, handler) => {
    source.on(event, handler); listeners.push(() => source.removeListener(event, handler));
  };
  const schedule = delay => {
    timers.clearTimeout(timer);
    if (!stopped && enabled && state.status !== 'ready') {
      timer = timers.setTimeout(() => { void check(); }, delay); timer?.unref?.();
    }
  };
  function fail() {
    if (stopped || state.status === 'ready') return;
    candidate = null;
    emit({status: 'error', error: '无法连接 GitHub 或验证更新，稍后会自动重试。'});
    schedule(Math.min(3600000, 60000 * 2 ** Math.min(failures++, 6)));
  }
  function check() {
    if (!enabled || stopped || ['ready', 'preparing'].includes(state.status)) return Promise.resolve(snapshot());
    if (pending) return pending;
    timers.clearTimeout(timer);
    pending = (async () => {
      emit({status: 'checking', error: null, progress: 0});
      try {
        candidate = null;
        const result = await updater.checkForUpdates();
        await result?.downloadPromise;
        if (!stopped && !['ready', 'preparing', 'error'].includes(state.status)) {
          failures = 0; schedule(6 * 60 * 60 * 1000);
        }
      } catch { if (state.status !== 'error') fail(); }
      finally { pending = null; }
      return snapshot();
    })();
    return pending;
  }
  function start() {
    if (started || stopped || !enabled) return;
    started = true;
    updater.autoDownload = true;
    updater.autoInstallOnAppQuit = true;
    updater.autoRunAppAfterInstall = false;
    // Community versions carry a suffix, but only stable GitHub Releases qualify.
    updater.allowPrerelease = false;
    updater.allowDowngrade = false;
    updater.disableDifferentialDownload = true;
    listen(updater, 'checking-for-update', () => emit({status: 'checking', error: null}));
    listen(updater, 'update-available', info => {
      candidate = info.version; emit({status: 'downloading', availableVersion: candidate, progress: 0, error: null});
    });
    listen(updater, 'update-not-available', () => emit({status: 'current', availableVersion: null, progress: 0, error: null}));
    listen(updater, 'download-progress', info => emit({status: 'downloading', progress: Math.round(info.percent)}));
    listen(updater, 'update-downloaded', info => {
      candidate = info.version; emit({status: 'preparing', availableVersion: candidate, progress: 100, error: null});
    });
    // Electron's native updater verifies and stages the signed Mac app after download.
    listen(nativeUpdater, 'update-downloaded', () => {
      if (!candidate) return;
      timers.clearTimeout(timer); failures = 0;
      emit({status: 'ready', availableVersion: candidate, progress: 100, error: null});
    });
    listen(updater, 'error', fail);
    schedule(initialDelay);
  }
  return {start, check, snapshot,
    install() {
      if (stopped || state.status !== 'ready') throw Error('更新尚未准备完成');
      app.quit();
    },
    stop() { stopped = true; timers.clearTimeout(timer); for (const remove of listeners.splice(0)) remove(); },
  };
}
module.exports = {createAppUpdates};

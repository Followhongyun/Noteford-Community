'use strict';

// This controller never touches identities or workspaces. Squirrel stages a
// verified app bundle, then installs it after the host exits normally.
function createAppUpdates({ updater, nativeUpdater, app, platform = process.platform,
  enabled = true, authorize = async () => ({}), changed = () => {},
  timers = { setTimeout, clearTimeout }, initialDelay = 15000 }) {
  const mac = platform === 'darwin';
  let state = { status: enabled ? 'idle' : 'unavailable', version: app.getVersion(),
    availableVersion: null, progress: 0, error: null };
  let activeCancellation;
  let timer, pending, stopped = false, started = false, failures = 0, candidate;
  let authorized = true;
  const listeners = [];
  const snapshot = () => ({ ...state });
  const emit = patch => { if (!stopped && authorized) { state = { ...state, ...patch }; changed(snapshot()); } };
  const listen = (source, event, handler) => {
    source.on(event, handler); listeners.push(() => source.removeListener(event, handler));
  };
  const schedule = delay => {
    timers.clearTimeout(timer);
    if (!stopped && enabled && state.status !== 'ready') {
      timer = timers.setTimeout(() => { void check(); }, delay);
      timer?.unref?.();
    }
  };
  function fail(error) {
    if (stopped || !authorized || state.status === 'ready') return;
    candidate = null;
    // Do not forward upstream URLs, headers or server bodies to the renderer.
    // A missing channel manifest is different from a failed artifact download.
    const reason = error?.code === 'ERR_UPDATER_CHANNEL_FILE_NOT_FOUND' ? 'update_feed_unpublished'
      : [401, 403].includes(error?.statusCode) ? 'update_access_denied'
      : ['ENOTFOUND', 'EAI_AGAIN', 'ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT'].includes(error?.code)
        || /^net::ERR_(INTERNET_DISCONNECTED|NAME_NOT_RESOLVED|CONNECTION_REFUSED|CONNECTION_RESET|CONNECTION_TIMED_OUT|TIMED_OUT)\b/.test(error?.message || '') ? 'update_network_unavailable'
      : 'update_failed';
    emit({ status: 'error', error: mac ? reason : '暂时无法更新，稍后会自动重试。' });
    schedule(reason === 'update_feed_unpublished' ? 6 * 60 * 60 * 1000
      : Math.min(60 * 60 * 1000, 60000 * 2 ** Math.min(failures++, 6)));
  }
  function check() {
    if (!enabled || stopped || !authorized || ['ready', 'preparing'].includes(state.status)) return Promise.resolve(snapshot());
    if (pending) return pending;
    timers.clearTimeout(timer);
    pending = (async () => {
      emit({ status: 'checking', error: null, progress: 0 });
      try {
        const headers = await authorize();
        if (stopped || !authorized) return snapshot();
        // No logged-in entitled account: retain local access, make no request.
        if (!headers) { emit({ status: 'idle', error: null }); schedule(60000); return snapshot(); }
        updater.requestHeaders = headers;
        candidate = null;
        const result = await updater.checkForUpdates();
        activeCancellation = result?.cancellationToken;
        if (stopped || !authorized) activeCancellation?.cancel();
        await result?.downloadPromise;
        if (!stopped && !['ready', 'preparing', 'error'].includes(state.status)) {
          failures = 0;
          schedule(6 * 60 * 60 * 1000);
        }
      } catch (error) { if (state.status !== 'error') fail(error); }
      finally { pending = null; activeCancellation = null; }
      return snapshot();
    })();
    return pending;
  }
  function start() {
    if (started || stopped || !enabled) return;
    started = true;
    updater.autoDownload = true;
    updater.autoInstallOnAppQuit = mac && authorized;
    updater.autoRunAppAfterInstall = !mac;
    updater.allowDowngrade = false;
    updater.allowPrerelease = false;
    listen(updater, 'checking-for-update', () => emit({ status: 'checking', error: null, progress: 0 }));
    listen(updater, 'update-available', info => {
      candidate = info.version;
      emit({ status: 'downloading', availableVersion: candidate, progress: 0, error: null });
    });
    listen(updater, 'update-not-available', () => emit({ status: 'current', availableVersion: null, progress: 0, error: null }));
    listen(updater, 'download-progress', progress => emit({ status: 'downloading', progress: Math.round(progress.percent) }));
    listen(updater, 'update-downloaded', info => {
      candidate = info.version;
      emit({ status: mac ? 'preparing' : 'ready', availableVersion: candidate, progress: 100, error: null });
    });
    if (mac) listen(nativeUpdater, 'update-downloaded', () => {
      if (!candidate) return;
      timers.clearTimeout(timer);
      failures = 0;
      emit({ status: 'ready', availableVersion: candidate, progress: 100, error: null });
    });
    listen(updater, 'error', fail);
    schedule(initialDelay);
  }
  return {
    start, check, snapshot,
    setAuthorized(value) {
      authorized = value;
      updater.autoInstallOnAppQuit = mac && value;
      if (!value) {
        activeCancellation?.cancel();
        updater.requestHeaders = null;
        candidate = null;
        timers.clearTimeout(timer);
        state = { ...state, status: enabled ? 'idle' : 'unavailable', availableVersion: null, progress: 0, error: null };
        changed(snapshot());
      } else if (started) schedule(initialDelay);
    },
    install() {
      if (stopped || !authorized || state.status !== 'ready') throw Error('更新尚未准备完成');
      // macOS: ordinary quit only. Never use native quitAndInstall, which relaunches.
      if (mac) app.quit();
      else updater.quitAndInstall(true, true);
    },
    stop() {
      stopped = true; activeCancellation?.cancel(); timers.clearTimeout(timer);
      for (const remove of listeners.splice(0)) remove();
      updater.requestHeaders = null;
    },
  };
}
module.exports = { createAppUpdates };

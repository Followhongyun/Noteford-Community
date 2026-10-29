const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createAppUpdates } = require('../electron/app-updates.cjs');
function fixture(t, options = {}) {
  const updater = new EventEmitter(), nativeUpdater = new EventEmitter(), timers = new Map();
  let checks = 0, quits = 0, installs = 0;
  updater.checkForUpdates = async () => { checks++; updater.emit('checking-for-update'); updater.emit('update-not-available'); };
  updater.quitAndInstall = () => installs++;
  const c = createAppUpdates({ updater, nativeUpdater,
    app: { getVersion: () => '0.1.0', quit: () => quits++ }, platform: 'darwin',
    timers: { setTimeout(fn, ms) { const id = {}; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } },
    ...options });
  t.after(() => c.stop()); c.start();
  return { c, updater, nativeUpdater, timers, counts: () => ({ checks, quits, installs }) };
}
test('background ZIP is not ready until native signature validation; only an explicit quit exits, never relaunches', async t => {
  const f = fixture(t);
  f.updater.emit('update-available', { version: '0.1.1' });
  f.updater.emit('update-downloaded', { version: '0.1.1' });
  assert.equal(f.c.snapshot().status, 'preparing');
  await f.c.check(); assert.equal(f.counts().checks, 0);
  assert.throws(() => f.c.install());
  f.nativeUpdater.emit('update-downloaded');
  assert.equal(f.c.snapshot().status, 'ready');
  assert.equal(f.updater.autoInstallOnAppQuit, true);
  assert.equal(f.updater.autoRunAppAfterInstall, false);
  assert.equal(f.updater.allowDowngrade, false);
  await f.c.check(); assert.deepEqual(f.counts(), { checks: 0, quits: 0, installs: 0 });
  f.c.install(); assert.deepEqual(f.counts(), { checks: 0, quits: 1, installs: 0 });
});
test('failed signature never marks ready; no secrets in UI, retries are bounded and cancelled on quit', t => {
  const f = fixture(t);
  f.updater.emit('update-downloaded', { version: '0.1.1' });
  f.updater.emit('error', Error('secret token and upstream response'));
  f.nativeUpdater.emit('update-downloaded');
  assert.equal(f.c.snapshot().status, 'error');
  assert.doesNotMatch(JSON.stringify(f.c.snapshot()), /secret|token/);
  assert.equal([...f.timers.values()][0].ms, 60000);
  for (let i = 0; i < 10; i++) f.updater.emit('error', Error('offline'));
  assert.equal(f.timers.size, 1);
  assert.equal([...f.timers.values()][0].ms, 3600000);
  assert.throws(() => f.c.install()); f.c.stop(); assert.equal(f.timers.size, 0);
  assert.equal(f.counts().quits, 0);
});
test('signed-out checks do not fetch; concurrent checks/downloads use one authorization and one request', async t => {
  let resolve, authorized = false, calls = 0;
  const f = fixture(t, { authorize: async () => { calls++; return authorized ? { authorization: 'Bearer fixture' } : null; } });
  await f.c.check(); assert.equal(f.counts().checks, 0);
  authorized = true;
  f.updater.checkForUpdates = async () => ({ downloadPromise: new Promise(r => { resolve = r; }) });
  const one = f.c.check(), two = f.c.check();
  assert.equal(one, two);
  await Promise.resolve(); assert.equal(calls, 2);
  resolve(); await one;
  assert.equal(f.updater.requestHeaders.authorization, 'Bearer fixture');
});
test('download rejection is handled without unhandled promise or logout; subsequent check can succeed', async t => {
  const f = fixture(t);
  f.updater.checkForUpdates = async () => ({ downloadPromise: Promise.reject(Error('checksum mismatch')) });
  await f.c.check(); assert.equal(f.c.snapshot().status, 'error');
  f.updater.checkForUpdates = async () => { f.updater.emit('update-not-available'); };
  await f.c.check(); assert.equal(f.c.snapshot().status, 'current');
  assert.equal([...f.timers.values()][0].ms, 6 * 3600000);
});
test('development does not fetch and Windows retains explicit restart behavior', async t => {
  const dev = fixture(t, { enabled: false });
  await dev.c.check(); assert.equal(dev.counts().checks, 0); assert.equal(dev.timers.size, 0);
  const win = fixture(t, { platform: 'win32' });
  win.updater.emit('update-downloaded', { version: '0.1.1' });
  assert.equal(win.updater.autoInstallOnAppQuit, false);
  win.c.install(); assert.equal(win.counts().installs, 1);
});
test('missing manifest is reported as unpublished and a later manual check can recover', async t => {
  const f = fixture(t);
  f.updater.checkForUpdates = async () => {
    throw Object.assign(Error('private URL and token'), { code: 'ERR_UPDATER_CHANNEL_FILE_NOT_FOUND' });
  };
  await f.c.check();
  assert.equal(f.c.snapshot().error, 'update_feed_unpublished');
  assert.equal([...f.timers.values()][0].ms, 6 * 3600000);
  assert.doesNotMatch(JSON.stringify(f.c.snapshot()), /private|token/);
  f.updater.checkForUpdates = async () => { throw Object.assign(Error('private URL'), {code: 'ENOTFOUND'}); };
  await f.c.check();
  assert.equal(f.c.snapshot().error, 'update_network_unavailable');
  f.updater.checkForUpdates = async () => { f.updater.emit('checking-for-update'); f.updater.emit('update-not-available'); };
  await f.c.check();
  assert.equal(f.c.snapshot().status, 'current');
  assert.equal(f.c.snapshot().error, null);
});
test('access and connection failures remain distinct; missing ZIP or signature failures are not unpublished feeds', t => {
  const f = fixture(t);
  for (const [error, expected] of [
    [{statusCode: 401}, 'update_access_denied'],
    [{statusCode: 403}, 'update_access_denied'],
    [{code: 'ENOTFOUND'}, 'update_network_unavailable'],
    [{message: 'net::ERR_INTERNET_DISCONNECTED private URL'}, 'update_network_unavailable'],
    [{statusCode: 404, message: 'missing private ZIP'}, 'update_failed'],
    [{code: 'ERR_UPDATER_INVALID_SIGNATURE'}, 'update_failed'],
  ]) {
    f.updater.emit('error', error);
    assert.equal(f.c.snapshot().error, expected);
    assert.equal(f.c.snapshot().status, 'error');
  }
});
test('private provider rejects artifact URLs outside the protected path', () => {
  const { PrivateUpdateProvider } = require('../electron/private-update-provider.cjs');
  const p = new PrivateUpdateProvider({ url: 'https://followhongyun.com/updates/noteford/macos/' }, {}, { platform: 'darwin' });
  const info = url => ({ version: '0.1.1', files: [{ url, sha512: 'fixture' }] });
  assert.equal(p.resolveFiles(info('Noteford-0.1.1-arm64.zip')).length, 1);
  for (const url of ['https://outside.test/a.zip', '../a.zip', '/public/a.zip', 'a.zip?token=secret'])
    assert.throws(() => p.resolveFiles(info(url)));
});
test('updater transport cannot send account credentials to redirects outside the configured origin', () => {
  const { configurePrivateUpdates } = require('../electron/private-update-provider.cjs');
  let called = 0;
  const updater = { setFeedURL() {}, httpExecutor: { createRequest() { called++; } } };
  configurePrivateUpdates(updater);
  const options = { protocol: 'https:', hostname: 'followhongyun.com', path: '/updates/noteford/macos/latest-mac.yml' };
  updater.httpExecutor.createRequest(options);
  assert.equal(called, 1);
  for (const patch of [{ hostname: 'outside.test' }, { protocol: 'http:' }, { path: '/public/file.zip' }, { port: 444 }])
    assert.throws(() => updater.httpExecutor.createRequest({ ...options, ...patch }));
  assert.equal(called, 1);
});

test('logout disables staged installation and ignores late download events until login', async t => {
  const f = fixture(t);
  f.updater.emit('update-downloaded', {version:'0.1.1'});
  f.nativeUpdater.emit('update-downloaded');
  f.c.setAuthorized(false);
  assert.equal(f.updater.autoInstallOnAppQuit, false);
  f.updater.emit('update-downloaded', {version:'0.1.1'});
  f.nativeUpdater.emit('update-downloaded');
  await f.c.check();
  assert.equal(f.counts().checks, 0);
  assert.equal(f.c.snapshot().status, 'idle');
  assert.throws(() => f.c.install());
  f.c.setAuthorized(true);
  await f.c.check();
  assert.equal(f.counts().checks, 1);
});

test('logout cancels an active transfer and does not show a download error',async t=>{
  const f=fixture(t);let cancelled=false,rejectDownload;
  f.updater.checkForUpdates=async()=>({cancellationToken:{cancel(){cancelled=true;rejectDownload(new Error('cancelled'));}},downloadPromise:new Promise((resolve,reject)=>{rejectDownload=reject;})});
  const pending=f.c.check();await new Promise(resolve=>setImmediate(resolve));
  f.c.setAuthorized(false);await pending;
  assert.equal(cancelled,true);assert.equal(f.c.snapshot().status,'idle');assert.equal(f.c.snapshot().error,null);
});

test('native provider routes a full ZIP fallback through verified ranges and rejects unlisted metadata',async t=>{
  const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
  const {Readable}=require('node:stream');const {CancellationToken}=require('builder-util-runtime');
  const {configurePrivateUpdates,PrivateUpdateProvider}=require('../electron/private-update-provider.cjs');
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'noteford-provider-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const bytes=Buffer.from('verified complete ZIP fixture'),sha512=crypto.createHash('sha512').update(bytes).digest('base64');
  let range,delegated=0;
  const updater={setFeedURL(){},httpExecutor:{download(){delegated++;},createRequest(options,callback){
    range=options.headers.Range;const req=new EventEmitter();req.abort=()=>{};
    req.end=()=>{const res=Readable.from([bytes]);res.statusCode=206;res.headers={'content-range':`bytes 0-${bytes.length-1}/${bytes.length}`,'content-length':String(bytes.length),etag:`"sha512-${Buffer.from(sha512,'base64').toString('hex')}"`};callback(res);};return req;
  }}};
  configurePrivateUpdates(updater);
  const provider=new PrivateUpdateProvider({url:'https://followhongyun.com/updates/noteford/macos/'},updater,{platform:'darwin',isUseMultipleRangeRequest:true});
  assert.equal(provider.isUseMultipleRangeRequest,false);
  const [file]=provider.resolveFiles({version:'0.1.5',files:[{url:'Noteford-0.1.5-arm64.zip',sha512,size:bytes.length}]});
  const destination=path.join(root,'pending','update.zip');fs.mkdirSync(path.dirname(destination));
  await updater.httpExecutor.download(file.url,destination,{sha512,headers:{authorization:'Bearer fixture'},cancellationToken:new CancellationToken()});
  assert.deepEqual(fs.readFileSync(destination),bytes);assert.equal(range,`bytes=0-${bytes.length-1}`);assert.equal(delegated,0);
  assert.throws(()=>updater.httpExecutor.download(new URL('https://followhongyun.com/updates/noteford/macos/unlisted.zip'),destination,{sha512}));
});

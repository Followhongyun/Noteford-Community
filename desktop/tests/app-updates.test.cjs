'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {createAppUpdates} = require('../electron/app-updates.cjs');
function fixture(enabled = true) {
  const updater = new EventEmitter(), nativeUpdater = new EventEmitter();
  let quit = 0, checks = 0;
  const delays = [];
  updater.checkForUpdates = async () => { checks++; updater.emit('update-not-available'); };
  const controller = createAppUpdates({updater, nativeUpdater, enabled,
    app: {getVersion: () => '0.1.5-community.1', quit: () => quit++},
    timers: {setTimeout: (_f, delay) => { delays.push(delay); return 1; }, clearTimeout() {}}});
  controller.start();
  return {updater, nativeUpdater, controller, delays, get quit() {return quit}, get checks() {return checks}};
}
test('public updater disables prereleases, downgrade and differential downloads', async () => {
  const f = fixture();
  assert.equal(f.updater.allowPrerelease, false); assert.equal(f.updater.allowDowngrade, false);
  assert.equal(f.updater.disableDifferentialDownload, true);
  assert.deepEqual(f.delays, [15000]);
  await f.controller.check(); assert.equal(f.controller.snapshot().status, 'current');
  assert.equal(f.delays.at(-1), 21600000);
});
test('only native verification permits quit/install; errors never install', () => {
  const f = fixture();
  f.updater.emit('update-available', {version:'0.1.5-community.2'});
  f.updater.emit('update-downloaded', {version:'0.1.5-community.2'});
  assert.equal(f.controller.snapshot().status, 'preparing');
  assert.throws(() => f.controller.install());
  f.nativeUpdater.emit('update-downloaded');
  assert.equal(f.controller.snapshot().status, 'ready');
  f.controller.install(); assert.equal(f.quit,1);
  const bad = fixture();
  bad.updater.emit('update-available', {version:'0.1.5-community.2'});
  bad.updater.emit('error', Error('sha512 mismatch / private details'));
  bad.nativeUpdater.emit('update-downloaded');
  assert.equal(bad.controller.snapshot().status,'error');
  assert.doesNotMatch(bad.controller.snapshot().error, /private|sha512/);
  assert.throws(() => bad.controller.install());
});
test('development never checks, stopped controller removes listeners', async () => {
  const f = fixture(false); await f.controller.check(); assert.equal(f.checks,0);
  const live = fixture(); live.controller.stop(); await live.controller.check();
  assert.equal(live.checks,0); assert.equal(live.nativeUpdater.listenerCount('update-downloaded'),0);
});
test('duplicate checks share one request, failed checks back off', async () => {
  const f=fixture(); let release;
  f.updater.checkForUpdates=()=>new Promise(resolve=>{release=resolve});
  const first=f.controller.check(); assert.equal(f.controller.check(),first); release(); await first;
  f.updater.checkForUpdates=async()=>{throw Error('network')};
  await f.controller.check(); assert.equal(f.delays.at(-1),60000);
  await f.controller.check(); assert.equal(f.delays.at(-1),120000);
});
test('packaged feed matches the public GitHub release configuration', () => {
  const fs = require('node:fs'), path = require('node:path');
  const yaml = require('js-yaml');
  const config = require('../package.json').build.mac.publish;
  const bundled = yaml.load(fs.readFileSync(path.join(__dirname,'../app-update.yml'),'utf8'));
  for (const key of ['provider','owner','repo','channel']) assert.equal(bundled[key],config[key]);
  assert.equal(bundled.provider,'github'); assert.equal(bundled.repo,'Noteford-Community');
  assert.equal(bundled.channel,'latest'); assert.equal(bundled.token,undefined);
  const semver = require('semver'); assert.equal(semver.gt('0.1.5-community.2','0.1.5-community.1'),true);
});

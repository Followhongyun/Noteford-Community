const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createRequire} = require('node:module');
const updaterRequire = createRequire(require.resolve('electron-updater'));
const {Lazy} = updaterRequire('lazy-val');
assert.equal(updaterRequire('lazy-val/package.json').name, '@noteford/lazy');

(async () => {
  let calls = 0;
  const item = new Lazy(() => {calls++; return Promise.resolve('configuration');});
  assert.equal(calls, 0);
  const [a, b] = await Promise.all([item.value, item.value]);
  assert.equal(a, b); assert.equal(calls, 1);
  const empty = new Lazy(() => {calls++; return undefined;});
  void empty.value; void empty.value; assert.equal(calls, 2);
  const rejected = new Lazy(() => Promise.reject(new Error('read failed')));
  await assert.rejects(rejected.value, /read failed/);
  assert.equal(rejected.value, rejected.value);

  // Exercise the real updater's configuration reload and persistent rollout ID.
  const {AppUpdater} = require('electron-updater/out/AppUpdater');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-updater-license-'));
  const app = {version: '0.3.8', userDataPath: dir, isPackaged: true};
  const updater = new AppUpdater(null, app);
  updater.logger = null;
  const config = path.join(dir, 'app-update.yml');
  fs.writeFileSync(config, 'provider: github\nowner: Followhongyun\nrepo: Noteford-Updates\n');
  updater.updateConfigPath = config;
  assert.equal((await updater.configOnDisk.value).repo, 'Noteford-Updates');
  fs.writeFileSync(config, 'provider: generic\nurl: https://example.invalid/updates\n');
  updater.updateConfigPath = config;
  assert.equal((await updater.configOnDisk.value).provider, 'generic');
  const ids = await Promise.all([updater.stagingUserIdPromise.value, updater.stagingUserIdPromise.value]);
  assert.equal(ids[0], ids[1]);
  assert.equal(fs.readFileSync(path.join(dir, '.updaterId'), 'utf8'), ids[0]);
  console.log('PASS: independent lazy adapter and real updater config/rollout integration');
})().catch(error => {console.error(error); process.exitCode = 1;});

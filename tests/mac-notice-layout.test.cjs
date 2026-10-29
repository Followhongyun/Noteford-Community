const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const asar = require('../desktop/node_modules/@electron/asar');
const {verifyMacNotices} = require('../desktop/scripts/mac-pack.cjs');

test('the real Mac file mapping bundles notices in ASAR and leaves no loose directory', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-notice-layout-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const source = path.join(root, 'build/third-party-licenses');
  const input = {'mac-ffmpeg.json': '{}', 'mac-silk/LICENSE.txt': 'fixture notice', 'mac-image-decoder/source.tar.xz': Buffer.from([0, 255, 7, 12])};
  for (const [name, data] of Object.entries(input)) {
    const file = path.join(source, name);
    fs.mkdirSync(path.dirname(file), {recursive: true});
    fs.writeFileSync(file, data);
  }
  fs.mkdirSync(path.join(root, 'desktop'));
  fs.writeFileSync(path.join(root, 'desktop/package.json'), '{"name":"fixture","version":"1.0.0"}');
  const desktop = path.resolve(__dirname, '../desktop');
  const app = path.join(root, 'Noteford 开源版.app');
  const resources = path.join(app, 'Contents/Resources');
  fs.mkdirSync(resources, {recursive: true});
  // Use electron-builder's installed matcher rather than reimplementing its
  // platform merge rules; all input and output are synthetic and isolated.
  const staged = spawnSync(process.execPath, ['-e', `
    const fs = require('node:fs'), path = require('node:path');
    const desktop = ${JSON.stringify(desktop)}, root = ${JSON.stringify(root)};
    const {getConfig} = require(path.join(desktop, 'node_modules/app-builder-lib/out/util/config/config.js'));
    const {getMainFileMatchers, getFileMatchers} = require(path.join(desktop, 'node_modules/app-builder-lib/out/fileMatcher.js'));
    const {computeFileSets} = require(path.join(desktop, 'node_modules/app-builder-lib/out/util/appFileCopier.js'));
    (async () => {
      const config = await getConfig(desktop), project = path.join(root, 'desktop');
      const destination = path.join(root, 'asar-input');
      const info = {projectDir: project, buildResourcesDir: path.join(project, 'build'), config,
        isPrepackedAppAsar: false, debugLogger: {isEnabled: false}, areNodeModulesHandledExternally: false};
      const matchers = getMainFileMatchers(project, destination, value => value, config.mac, {info}, path.join(root, 'out'), false);
      const sets = await computeFileSets(matchers, null, {info}, false);
      for (const set of sets) for (const file of set.files) {
        if (!fs.statSync(file).isFile()) continue;
        const target = path.join(set.destination, path.relative(set.src, file));
        fs.mkdirSync(path.dirname(target), {recursive: true}); fs.copyFileSync(file, target);
      }
      const loose = getFileMatchers(config, 'extraResources', path.join(root, 'resources'), {
        macroExpander: value => value, customBuildOptions: config.mac,
        globalOutDir: path.join(root, 'out'), defaultSrc: project,
      });
      if (loose.some(item => item.to.endsWith('third-party-licenses'))) throw Error('Loose notice mapping remains');
    })().catch(error => {console.error(error); process.exitCode=1;});
  `], {encoding: 'utf8'});
  assert.equal(staged.status, 0, staged.stdout + staged.stderr);
  const archive = path.join(resources, 'app.asar');
  await asar.createPackage(path.join(root, 'asar-input'), archive);
  assert.deepEqual(verifyMacNotices(app, source), {noticeFiles: 3});
  assert.equal(fs.existsSync(path.join(resources, 'third-party-licenses')), false);

  fs.writeFileSync(path.join(source, 'mac-silk/LICENSE.txt'), 'different notice');
  assert.throws(() => verifyMacNotices(app, source), /notice missing, unpacked or changed/);
  fs.writeFileSync(path.join(source, 'mac-silk/LICENSE.txt'), input['mac-silk/LICENSE.txt']);
  fs.mkdirSync(path.join(resources, 'third-party-licenses'));
  assert.throws(() => verifyMacNotices(app, source), /not a separate resource directory/);
});

test('Mac notice verification rejects missing and unpacked archive entries', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-notice-failure-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const source = path.join(root, 'source'), input = path.join(root, 'input');
  const app = path.join(root, 'Noteford 开源版.app'), resources = path.join(app, 'Contents/Resources');
  fs.mkdirSync(source); fs.mkdirSync(resources, {recursive: true});
  fs.mkdirSync(path.join(input, 'licenses/runtime'), {recursive: true});
  fs.writeFileSync(path.join(input, 'package.json'), '{"name":"fixture"}');
  fs.writeFileSync(path.join(source, 'LICENSE.txt'), 'fixture');
  fs.writeFileSync(path.join(input, 'licenses/runtime/LICENSE.txt'), 'fixture');
  const archive = path.join(resources, 'app.asar');
  await asar.createPackageWithOptions(input, archive, {unpack: '**/LICENSE.txt'});
  assert.throws(() => verifyMacNotices(app, source), /notice missing, unpacked or changed/);
  asar.uncache(archive);
  fs.rmSync(path.join(input, 'licenses/runtime/LICENSE.txt'));
  await asar.createPackage(input, archive);
  assert.throws(() => verifyMacNotices(app, source), /LICENSE.txt|not found|Cannot read/);
});

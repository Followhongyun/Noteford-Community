const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function hash(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}
function ensureMacUpdateConfig(app) {
  const file = path.join(app, 'Contents', 'Resources', 'app-update.yml');
  // electron-builder omits this file for --dir, but electron-updater still
  // reads it to create its download cache even with a custom feed provider.
  if (!fs.existsSync(file)) {
    const config = require('../package.json');
    fs.writeFileSync(file, JSON.stringify({
      ...config.build.mac.publish,
      updaterCacheDirName: `${config.name}-updater`,
    }, null, 2) + '\n');
  }
}
function checkedPath(root, relative) {
  const file = path.resolve(root, relative);
  if (!file.startsWith(path.resolve(root) + path.sep)) throw Error('Invalid license manifest path');
  return file;
}
function relativeFiles(root) {
  const result = [];
  function visit(directory) {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
      const file = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) throw Error('Unexpected symlink in packaged service');
      if (entry.isDirectory()) visit(file);
      else result.push(path.relative(root, file).split(path.sep).join('/'));
    }
  }
  visit(root);
  return result.sort();
}
function verify(appOutDir, { requireClearance = true } = {}) {
  const resources = path.join(appOutDir, 'resources');
  const directory = path.join(resources, 'third-party-licenses');
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'manifest.json'), 'utf8'));
  if (manifest.schema_version !== 1 || !Object.keys(manifest.files || {}).length || !Object.keys(manifest.service_files || {}).length) {
    throw Error('Missing license inventory');
  }
  for (const [name, expected] of Object.entries(manifest.files)) {
    if (hash(checkedPath(directory, name)) !== expected) throw Error('License file missing or changed: ' + name);
  }
  const service = path.join(resources, 'service');
  if (JSON.stringify(relativeFiles(service)) !== JSON.stringify(Object.keys(manifest.service_files).sort())) {
    throw Error('Packaged service file set differs from audited inventory');
  }
  for (const [name, expected] of Object.entries(manifest.service_files)) {
    if (hash(checkedPath(service, name)) !== expected) throw Error('Service differs from audited binary: ' + name);
  }
  const expectedRuntime = [['LICENSE.electron.txt', 'runtime/LICENSE.electron.txt'], ['LICENSES.chromium.html', 'runtime/LICENSES.chromium.html']];
  for (const [name, copy] of expectedRuntime) {
    if (hash(path.join(appOutDir, name)) !== manifest.files[copy]) throw Error('Electron notice missing or differs: ' + name);
  }
  if (requireClearance && (!manifest.commercial_clearance || manifest.unresolved?.length)) {
    throw Error('Commercial distribution license review is incomplete. See resources/third-party-licenses/INDEX.md');
  }
  return {packages: manifest.entries.length, noticeFiles: Object.keys(manifest.files).length,
    commercialClearance: manifest.commercial_clearance, unresolved: manifest.unresolved};
}

module.exports = async context => {
  if (context && context.electronPlatformName === 'darwin') {
    const pack = require('./mac-pack.cjs');
    const app = pack.macAppBundle(context.appOutDir, fs.existsSync, directory => fs.readdirSync(directory));
    require('./verify-runtime-dependencies.cjs')(path.join(app, 'Contents', 'Resources', 'app.asar'));
    // Signing changes Mach-O bytes, including libffmpeg's signature region.
    const verified = pack.verifyMacPack(context);
    require('./mac-uninstaller.cjs').build(app);
    ensureMacUpdateConfig(app);
    pack.signMacApp(app, {
      teamId: require('../package.json').notefordMacSigningTeamId,
      entitlements: path.join(__dirname, '..', 'entitlements.mac.plist'),
    });
    return verified;
  }
  const directory = path.join(context.appOutDir, 'resources', 'third-party-licenses');
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'manifest.json'), 'utf8'));
  const desktop = path.resolve(__dirname, '..');
  if (hash(path.join(desktop, 'package-lock.json')) !== manifest.npm_lock_sha256) throw Error('npm lockfile changed since notice collection');
  if (JSON.parse(fs.readFileSync(path.join(desktop, 'package.json'), 'utf8')).version !== manifest.app_version) throw Error('App version differs from notice inventory');
  const provenance = JSON.parse(fs.readFileSync(path.join(directory, 'upstream/electron-build/binary-provenance.json'), 'utf8'));
  if (hash(path.join(context.appOutDir, 'ffmpeg.dll')) !== provenance.ffmpeg_sha256) throw Error('FFmpeg differs from reviewed official binary');
  const {extractFile, listPackage} = require('@electron/asar');
  const archive = path.join(context.appOutDir, 'resources', 'app.asar');
  const lazy = JSON.parse(extractFile(archive, path.join('node_modules', 'lazy-val', 'package.json')).toString());
  if (lazy.name !== '@noteford/lazy' || lazy.version !== '1.0.6') throw Error('Unreviewed lazy-val implementation packaged');
  const implementation = extractFile(archive, path.join('node_modules', 'lazy-val', 'index.cjs'));
  if (!implementation.equals(fs.readFileSync(path.join(desktop, 'vendor/noteford-lazy/index.cjs')))) throw Error('Lazy adapter differs from reviewed source');
  if (listPackage(archive).some(name => /node_modules[\\/]lazy-val[\\/].*\.js$/.test(name))) throw Error('Unexpected upstream lazy-val code packaged');
  if (hash(path.join(desktop, '../packaging/DISTRIBUTION-TERMS.txt')) !== manifest.files['DISTRIBUTION-TERMS.txt']) throw Error('Installer terms differ from bundled terms');
  return verify(context.appOutDir);
};
module.exports.verify = verify;
module.exports.ensureMacUpdateConfig = ensureMacUpdateConfig;
if (require.main === module) {
  const folder = process.argv[2];
  if (!folder) throw Error('Usage: node verify-third-party-licenses.cjs <unpacked-app> [--inventory-only]');
  console.log(JSON.stringify(verify(folder, {requireClearance: !process.argv.includes('--inventory-only')}), null, 2));
}

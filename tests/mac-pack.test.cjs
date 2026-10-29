const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const pack = require('../desktop/scripts/mac-pack.cjs');

// These synthetic PyInstaller outputs have a Unix executable bit even when
// the test host's filesystem does not support one. No fixture is executed.
function executableFixtureStat(file) {
  const value = fs.statSync(file);
  return { mode: value.mode | 0o111 };
}

function writeMachO(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const buf = Buffer.alloc(32);
  buf.writeUInt32LE(0xFEEDFACF, 0);
  buf.writeUInt32LE(pack.CPU_TYPE_ARM64, 4);
  buf.writeUInt32LE(/\.(dylib|node|so)$/.test(file) ? 6 : 2, 12);
  fs.writeFileSync(file, buf);
}

test('a directory-only Mac build can initialize the real updater download cache', async () => {
  const {ensureMacUpdateConfig} = require('../desktop/scripts/verify-third-party-licenses.cjs');
  const {AppUpdater} = require('../desktop/node_modules/electron-updater/out/AppUpdater');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-update-config-'));
  try {
    const app = path.join(root, 'Noteford.app');
    const resources = path.join(app, 'Contents', 'Resources');
    fs.mkdirSync(resources, {recursive: true});
    ensureMacUpdateConfig(app);
    const config = path.join(resources, 'app-update.yml');
    const updater = new AppUpdater(undefined, {
      version: '0.1.0', name: 'Noteford', isPackaged: true,
      appUpdateConfigPath: config, baseCachePath: path.join(root, 'cache'),
    });
    updater.logger = null;
    assert.equal((await updater.getOrCreateDownloadHelper()).cacheDir,
      path.join(root, 'cache', 'noteford-desktop-updater'));
    assert.equal((await updater.configOnDisk.value).url, 'https://followhongyun.com/updates/noteford/macos/');
    fs.writeFileSync(config, 'provider: generic\nurl: https://example.test/existing-builder-config\n');
    ensureMacUpdateConfig(app);
    assert.match(fs.readFileSync(config, 'utf8'), /existing-builder-config/);
  } finally { fs.rmSync(root, {recursive: true, force: true}); }
});

test('ad-hoc sign uses the Noteford identifier and automation entitlements', () => {
  // afterPack owns the complete inside-out Developer ID signing pass. Prevent
  // builder from overwriting only some seals with a different identity.
  const config = require('../desktop/package.json');
  assert.equal(config.build.mac.identity, null);
  const entitlements = path.join('desktop', 'entitlements.mac.plist');
  const target = path.join('/Applications/Noteford.app/Contents/MacOS/Noteford');
  const args = pack.adhocSignArguments(target, {
    identifier: 'com.followhongyun.noteford',
    entitlements,
  });
  assert.deepEqual(args, [
    '--force', '--sign', '-', '--identifier', 'com.followhongyun.noteford',
    '--entitlements', entitlements, '--options', 'runtime', target,
  ]);
  assert.equal(args.includes('--deep'), false);
  assert.equal(pack.adhocSignArguments(path.join('/tmp', 'libffmpeg.dylib')).includes('--identifier'), false);
  assert.deepEqual(pack.verifySignatureArguments(target), ['--verify', '--deep', '--strict', target]);
  assert.throws(() => pack.resignAdhocApp('', { execFileSync() {} }));
  const verify = fs.readFileSync(path.join(__dirname, '../desktop/scripts/verify-third-party-licenses.cjs'), 'utf8');
  const resignAt = verify.indexOf('signMacApp');
  const verifyAt = verify.indexOf('verifyMacPack');
  assert.ok(verifyAt >= 0 && resignAt > verifyAt);
});

test('ad-hoc sign walks inside-out and fails when the deep signature check fails', () => {
  const entitlements = path.join('desktop', 'entitlements.mac.plist');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-sign-'));
  const app = path.join(root, 'Noteford.app');
  const framework = path.join(app, 'Contents', 'Frameworks', 'Electron Framework.framework');
  writeMachO(path.join(app, 'Contents', 'MacOS', 'Noteford'));
  writeMachO(path.join(framework, 'Versions', 'A', 'Electron Framework'));
  writeMachO(path.join(framework, 'Versions', 'A', 'Libraries', 'libffmpeg.dylib'));
  writeMachO(path.join(framework, 'Versions', 'A', 'Helpers', 'chrome_crashpad_handler'));
  writeMachO(path.join(framework, 'Versions', 'A', 'Helpers', 'Inner Helper.app', 'Contents', 'MacOS', 'Inner Helper'));
  writeMachO(path.join(app, 'Contents', 'Frameworks', 'Noteford Helper.app', 'Contents', 'MacOS', 'Noteford Helper'));
  writeMachO(path.join(app, 'Contents', 'Resources', 'app.asar.unpacked', 'node_modules', 'koffi', 'build', 'koffi.node'));
  writeMachO(path.join(app, 'Contents', 'Resources', 'service', 'noteford-service'));
  writeMachO(path.join(app, 'Contents', 'Resources', 'service', '_internal', 'libcrypto.dylib'));
  writeMachO(path.join(app, 'Contents', 'Resources', 'service', '_internal', 'lib-dynload', 'foo.so'));
  fs.writeFileSync(path.join(app, 'Contents', 'Info.plist'), '<plist/>');
  fs.symlinkSync('A', path.join(framework, 'Versions', 'Current'));
  const calls = [];
  const signed = pack.resignAdhocApp(app, {
    entitlements,
    execFileSync: (command, argv) => { calls.push([command, argv]); },
  });
  const signCalls = calls.filter(call => call[1][0] !== '--verify');
  const targets = signCalls.map(call => call[1].at(-1));
  const at = suffix => targets.findIndex(item => item === suffix || item.endsWith(suffix));
  assert.equal(signCalls.every(call => call[0] === '/usr/bin/codesign'), true);
  assert.equal(signCalls.every(call => call[1].includes('--deep') === false), true);
  assert.ok(at(path.join('Libraries', 'libffmpeg.dylib')) < at('Electron Framework.framework'));
  assert.ok(at('chrome_crashpad_handler') < at('Electron Framework.framework'));
  assert.ok(at('Inner Helper.app') < at('Electron Framework.framework'));
  assert.ok(at('koffi.node') < at(app));
  assert.ok(at('noteford-service') < at(app));
  assert.ok(at('libcrypto.dylib') < at(app));
  assert.ok(at(path.join('lib-dynload', 'foo.so')) < at(app));
  assert.ok(at('Electron Framework.framework') < at('Noteford Helper.app'));
  assert.ok(at('Noteford Helper.app') < at(app));
  assert.equal(targets.at(-1), app);
  assert.equal(targets.some(item => item.endsWith(path.join('Contents', 'MacOS', 'Noteford'))), false);
  assert.equal(targets.some(item => item.endsWith('Current')), false);
  assert.equal(targets.includes(path.join(app, 'Contents', 'Info.plist')), false);
  const dylib = signCalls.find(call => call[1].at(-1).endsWith('libffmpeg.dylib'))[1];
  assert.equal(dylib.includes('--identifier'), false);
  assert.equal(dylib.includes('--entitlements'), false);
  const service = signCalls.find(call => call[1].at(-1).endsWith('noteford-service'))[1];
  assert.equal(service[service.indexOf('--entitlements') + 1], entitlements);
  const helper = signCalls.find(call => call[1].at(-1).endsWith('Noteford Helper.app'))[1];
  assert.equal(helper.includes('--identifier'), false);
  assert.equal(helper[helper.indexOf('--entitlements') + 1], entitlements);
  const outer = signCalls.find(call => call[1].at(-1) === app)[1];
  assert.equal(outer[outer.indexOf('--identifier') + 1], 'com.followhongyun.noteford');
  assert.equal(outer[outer.indexOf('--entitlements') + 1], entitlements);
  assert.equal(outer.includes('Electron'), false);
  const checked = calls.filter(call => call[1][0] === '--verify');
  assert.equal(checked.length, 1);
  assert.deepEqual(checked[0][1], ['--verify', '--deep', '--strict', app]);
  assert.equal(signed.identifier, 'com.followhongyun.noteford');
  assert.throws(() => pack.resignAdhocApp(app, {
    entitlements,
    execFileSync: (command, argv) => {
      if (argv[0] === '--verify') {
        const error = new Error('different Team IDs');
        error.stderr = Buffer.from('code signature in Electron Framework.framework not valid');
        throw error;
      }
    },
  }), /verify --deep --strict/);
  fs.rmSync(root, { recursive: true, force: true });
});

test('Mac codesign fixture has the Noteford identity and Apple Events entitlement without launching it', {
  skip: process.platform !== 'darwin' || process.env.NOTEFORD_TEST_CODESIGN !== '1',
}, t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-sign-fixture-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const app = path.join(root, 'Noteford.app');
  const helper = path.join(app, 'Contents', 'Frameworks', 'Fixture Helper.app');
  for (const bundle of [app, helper]) {
    const macos = path.join(bundle, 'Contents', 'MacOS');
    fs.mkdirSync(macos, { recursive: true });
    // Copy a CLI Mach-O solely as signing input. Never execute this fixture.
    fs.copyFileSync('/usr/bin/true', path.join(macos, 'fixture'));
    fs.chmodSync(path.join(macos, 'fixture'), 0o755);
    fs.writeFileSync(path.join(bundle, 'Contents', 'Info.plist'), `<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>fixture</string>
<key>CFBundleIdentifier</key><string>com.followhongyun.noteford${bundle === helper ? '.helper' : ''}</string>
<key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>`);
  }
  pack.resignAdhocApp(app);
  const identity = spawnSync('/usr/bin/codesign', ['-dv', app], { encoding: 'utf8' });
  assert.equal(identity.status, 0, identity.stderr);
  assert.match(identity.stderr, /^Identifier=com\.followhongyun\.noteford$/m);
  const requirement = spawnSync('/usr/bin/codesign', ['-d', '-r-', app], { encoding: 'utf8' });
  assert.equal(requirement.status, 0, requirement.stderr);
  assert.match(requirement.stdout + requirement.stderr, /designated => cdhash/);
  // Do not weaken trust to an identifier-only DR just to suppress TCC prompts.
  const helperIdentity = spawnSync('/usr/bin/codesign', ['-dv', helper], { encoding: 'utf8' });
  assert.match(helperIdentity.stderr, /^Signature=adhoc$/m);
  const entitlements = spawnSync('/usr/bin/codesign', ['-d', '--entitlements', '-', app], { encoding: 'utf8' });
  assert.equal(entitlements.status, 0, entitlements.stderr);
  assert.match(entitlements.stdout, /(?:<key>com\.apple\.security\.automation\.apple-events<\/key>\s*<true\/>|\[Key\] com\.apple\.security\.automation\.apple-events\s*\[Value\]\s*\[Bool\] true)/);
  assert.match(entitlements.stdout, /com\.apple\.security\.cs\.disable-library-validation/);
});

test('afterPack verifies original bytes before signing and propagates either failure', async t => {
  const hook = require('../desktop/scripts/verify-third-party-licenses.cjs');
  const calls = [];
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-after-pack-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const app = path.join(root, 'Noteford.app');
  fs.mkdirSync(path.join(app, 'Contents/Resources'), { recursive: true });
  t.mock.method(pack, 'macAppBundle', () => app);
  const dependenciesPath = require.resolve('../desktop/scripts/verify-runtime-dependencies.cjs');
  require(dependenciesPath);
  t.mock.method(require.cache[dependenciesPath], 'exports', () => { calls.push('dependencies'); });
  t.mock.method(require('../desktop/scripts/mac-uninstaller.cjs'), 'build', () => { calls.push('uninstaller'); });
  t.mock.method(pack, 'verifyMacPack', () => { calls.push('hash'); return { sha256: 'before-signing' }; });
  t.mock.method(pack, 'signMacApp', (_app, options) => {
    calls.push('sign');
    assert.ok(fs.existsSync(path.join(_app, 'Contents/Resources/app-update.yml')));
    assert.match(options.entitlements, /entitlements\.mac\.plist$/);
    assert.equal(options.teamId, 'QD3H9VHB23');
  });
  const context = { electronPlatformName: 'darwin', appOutDir: root };
  assert.deepEqual(await hook(context), { sha256: 'before-signing' });
  assert.deepEqual(calls, ['dependencies', 'hash', 'uninstaller', 'sign']);
  pack.signMacApp.mock.mockImplementation(() => { throw new Error('signature invalid'); });
  await assert.rejects(hook(context), /signature invalid/);
  calls.length = 0;
  pack.verifyMacPack.mock.mockImplementation(() => { throw new Error('ffmpeg changed'); });
  await assert.rejects(hook(context), /ffmpeg changed/);
  assert.deepEqual(calls, ['dependencies']);
});

test('release signing selects only the configured Developer ID team and never falls back to ad-hoc', () => {
  const teamId = 'QD3H9VHB23';
  const first = 'A'.repeat(40);
  const second = 'B'.repeat(40);
  let listing = `1) ${first} "Developer ID Application: Example (${teamId})"\n2) ${second} "Local Dev"`;
  const options = { teamId, env: {}, execFileSync: () => listing };
  assert.equal(pack.resolveDeveloperIdentity(options), first);
  listing += `\n3) ${second} "Developer ID Application: Renewed (${teamId})"`;
  assert.throws(() => pack.resolveDeveloperIdentity(options), /Ad-hoc fallback is disabled/);
  assert.equal(pack.resolveDeveloperIdentity({ ...options, env: { CSC_NAME: second } }), second);
  assert.throws(() => pack.resolveDeveloperIdentity({ ...options, teamId: 'OTHERTEAM1' }), /Ad-hoc fallback is disabled/);
  assert.throws(() => pack.resolveDeveloperIdentity({ ...options, env: { CSC_NAME: '-' } }), /Ad-hoc fallback is disabled/);
  listing = '';
  assert.throws(() => pack.resolveDeveloperIdentity(options), /Ad-hoc fallback is disabled/);
  const args = pack.codeSignArguments('/fixture.app', { identity: first, identifier: 'com.followhongyun.noteford' });
  assert.ok(args.includes('--timestamp=http://timestamp.apple.com/ts01'));
  assert.ok(!args.includes('--requirements'));
  assert.ok(!args.includes('--deep'));
});

test('Developer ID survives changed resources with the same certificate-bound requirement', {
  skip: process.platform !== 'darwin' || process.env.NOTEFORD_TEST_DEVELOPER_ID !== '1',
}, t => {
  const { execFileSync } = require('node:child_process');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-developer-id-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const app = path.join(root, 'Noteford.app');
  fs.mkdirSync(path.join(app, 'Contents', 'MacOS'), { recursive: true });
  fs.mkdirSync(path.join(app, 'Contents', 'Resources'));
  fs.copyFileSync('/usr/bin/true', path.join(app, 'Contents', 'MacOS', 'fixture'));
  fs.chmodSync(path.join(app, 'Contents', 'MacOS', 'fixture'), 0o755);
  fs.writeFileSync(path.join(app, 'Contents', 'Info.plist'), `<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>CFBundleExecutable</key><string>fixture</string>
<key>CFBundleIdentifier</key><string>com.followhongyun.noteford</string>
<key>CFBundlePackageType</key><string>APPL</string></dict></plist>`);
  const requirements = [];
  const hashes = [];
  for (const revision of ['first build', 'updated build']) {
    fs.writeFileSync(path.join(app, 'Contents', 'Resources', 'revision.txt'), revision);
    pack.signMacApp(app, { teamId: require('../desktop/package.json').notefordMacSigningTeamId });
    requirements.push(execFileSync('/usr/bin/codesign', ['-d', '-r-', app], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
    const details = spawnSync('/usr/bin/codesign', ['-dv', '--verbose=4', app], { encoding: 'utf8' });
    assert.equal(details.status, 0, details.stderr);
    assert.match(details.stderr, /^Authority=Developer ID Application:/m);
    assert.match(details.stderr, /^TeamIdentifier=QD3H9VHB23$/m);
    hashes.push(details.stderr.match(/^CDHash=(.+)$/m)[1]);
  }
  assert.notEqual(hashes[0], hashes[1]);
  assert.equal(requirements[0], requirements[1]);
});

test('timestamp retry stays bounded and cannot fall back to unsigned or ad-hoc', () => {
  const options = { identity: 'A'.repeat(40) };
  const calls = [];
  const transient = Object.assign(new Error('TSA unavailable'), { stderr: Buffer.from('The timestamp service is not available.') });
  pack.signTarget('/fixture.app', options, (_command, args) => {
    calls.push(args);
    if (calls.length < 3) throw transient;
  });
  assert.equal(calls.length, 3);
  assert.ok(calls.every(args => JSON.stringify(args) === JSON.stringify(calls[0])));
  let failures = 0;
  assert.throws(() => pack.signTarget('/fixture.app', options, () => { failures++; throw transient; }), /TSA unavailable/);
  assert.equal(failures, 5);
  failures = 0;
  assert.throws(() => pack.signTarget('/fixture.app', options, () => { failures++; throw new Error('key unavailable'); }), /key unavailable/);
  assert.equal(failures, 1);
});

test('Windows pack python stays on Scripts/python.exe', () => {
  const root = path.join('C:', 'src');
  assert.equal(pack.resolvePackPython(root, 'win32', () => true), path.join(root, '.venv-build', 'Scripts', 'python.exe'));
  const source = fs.readFileSync(path.join(__dirname, '../desktop/scripts/collect-third-party-licenses.cjs'), 'utf8');
  assert.match(source, /\.venv-build', 'Scripts', 'python\.exe'/);
  assert.match(source, /prepare-license-sources\.py/);
  assert.match(source, /collect-third-party-licenses\.py/);
  assert.match(source, /process\.platform === 'darwin'/);
  const verify = fs.readFileSync(path.join(__dirname, '../desktop/scripts/verify-third-party-licenses.cjs'), 'utf8');
  assert.match(verify, /ffmpeg\.dll/);
  assert.match(verify, /electronPlatformName === 'darwin'/);
});

function arm64Header() {
  const buf = Buffer.alloc(64);
  buf.writeUInt32LE(0xFEEDFACF, 0);
  buf.writeUInt32LE(pack.CPU_TYPE_ARM64, 4);
  return buf;
}

test('Mac build requirements keep the shared pins and drop Windows-only packages', () => {
  const root = path.resolve(__dirname, '..');
  const windows = fs.readFileSync(path.join(root, 'requirements-build.txt'), 'utf8');
  const mac = fs.readFileSync(path.join(root, 'requirements-build-mac.txt'), 'utf8');
  const pins = text => text.split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith('#'));
  const macPins = new Set(pins(mac));
  for (const line of pins(windows)) {
    const name = line.split('==')[0].toLowerCase();
    // Non-Mac downloads use the reviewed certifi bundle; Mac uses native trust.
    if (pack.WINDOWS_ONLY_REQUIREMENTS.includes(name) || name === 'certifi') assert.equal(macPins.has(line), false);
    else assert.equal(macPins.has(line), true, line);
  }
  assert.match(mac, /^cryptography==/m);
  assert.match(mac, /^pyinstaller==/m);
  assert.doesNotMatch(mac, /^pywin32==/m);
  assert.equal(pack.resolvePackPython(root, 'darwin'), path.join(root, '.venv-build', 'bin', 'python3'));
  const args = pack.pyinstallerArgs(root);
  assert.deepEqual(args.slice(0, 8), ['-m', 'PyInstaller', '--noconfirm', '--onedir', '--name', 'noteford-service', '--target-architecture', 'arm64']);
  assert.ok(args.includes(path.join(root, 'packaging', 'service-entry.py')));
  assert.equal(args[args.indexOf('--add-binary') + 1], `${path.join(root, 'build', 'mac-native', 'libwechat_key_hook.dylib')}:mac-native`);
  assert.match(pack.SERVICE_IMPORTS, /cryptography/);
});

test('Mac beforePack freezes an arm64 service and refuses a python3 launcher', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-mac-pack-'));
  const venv = path.join(root, '.venv-build', 'bin', 'python3');
  const requirements = path.join(root, 'requirements-build-mac.txt');
  const binary = pack.serviceBinary(root);
  const calls = [];
  let venvReady = false;
  fs.mkdirSync(path.dirname(binary), {recursive: true});
  fs.writeFileSync(binary, '#!/bin/sh\nexec python3 -m noteford.service "$@"\n');
  const io = {
    exists(target) {
      if (target === venv) return venvReady;
      if (target === requirements) return true;
      return fs.existsSync(target);
    },
    execFileSync(python, args) {
      calls.push([python, args]);
      if (args[0] === '-m' && args[1] === 'venv') {
        venvReady = true;
        return;
      }
      if (args[1] === 'pip') {
        assert.equal(python, venv);
        assert.ok(args.includes('--require-virtualenv'));
        assert.ok(args.includes(requirements));
        return;
      }
      if (args[0] === '-I' && args[1] === path.join(root, 'Scripts', 'bootstrap-build-pip.py')) {
        assert.equal(python, venv);
        return;
      }
      if (args[0] === '-c') {
        assert.match(args[1], /cryptography|build_hook|build_silk|build_images/);
        return;
      }
      if (args[1] === 'PyInstaller') {
        assert.equal(python, venv);
        assert.equal(fs.existsSync(binary), false);
        fs.mkdirSync(path.join(path.dirname(binary), '_internal'), {recursive: true});
        fs.writeFileSync(binary, arm64Header(), {mode: 0o755});
        writeMachO(path.join(path.dirname(binary), '_internal', 'mac-native', 'libwechat_key_hook.dylib'));
        writeMachO(path.join(path.dirname(binary), '_internal', 'mac-native', 'noteford-silk'));
        for (const helper of ['noteford-ffmpeg', 'noteford-ffprobe']) writeMachO(path.join(path.dirname(binary), '_internal', 'mac-native', helper));
        return;
      }
      throw new Error('unexpected ' + args.join(' '));
    },
    readFile: file => fs.readFileSync(file),
    stat: executableFixtureStat,
    rm(directory) { fs.rmSync(directory, {recursive: true, force: true}); },
  };
  const prepared = pack.prepareMacPack(root, io);
  assert.equal(prepared.python, venv);
  assert.equal(prepared.binary, binary);
  assert.equal(calls[0][0], 'python3');
  assert.deepEqual(calls[0][1].slice(0, 2), ['-m', 'venv']);
  const bootstrapAt = calls.findIndex(([, args]) => args[0] === '-I');
  const installAt = calls.findIndex(([, args]) => args[1] === 'pip');
  assert.ok(bootstrapAt > 0 && bootstrapAt < installAt);
  assert.equal(pack.isArm64MachO(fs.readFileSync(binary)), true);
  assert.equal(pack.isPythonServiceLauncher(fs.readFileSync(binary)), false);
  const compiledAt = calls.findIndex(([, args]) => args[0] === '-c' && args[1].includes('build_hook'));
  assert.ok(compiledAt >= 0 && compiledAt < calls.findIndex(([, args]) => args[1] === 'PyInstaller'));
  const note = JSON.parse(fs.readFileSync(path.join(root, 'build', 'third-party-licenses', 'mac-ffmpeg.json'), 'utf8'));
  assert.equal(note.ffmpeg, 'electron-prebuilt-libffmpeg.dylib');
  assert.equal(note.service, 'pyinstaller-onedir-arm64');
  assert.equal(note.source_archive, null);

  fs.writeFileSync(binary, '#!/bin/sh\nexec python3 -m noteford.service "$@"\n', {mode: 0o755});
  assert.throws(() => pack.assertFrozenMacService(binary, fs.readFileSync, fs.statSync, fs.existsSync), /python3 launcher/);
  const intel = Buffer.alloc(32);
  intel.writeUInt32LE(0xFEEDFACF, 0);
  intel.writeUInt32LE(0x01000007, 4);
  assert.equal(pack.isArm64MachO(intel), false);
  const removed = [];
  assert.throws(() => pack.freezeMacService(root, {
    exists: target => target === venv || target === requirements || target === binary,
    execFileSync(python, args) {
      if (args[1] === 'PyInstaller') throw new Error('codesign boom');
    },
    readFile: () => Buffer.from('#!/bin/sh\nexec python3 -m noteford.service "$@"\n'),
    stat: () => ({mode: 0o755}),
    rm(directory) { removed.push(directory); },
  }), /codesign boom/);
  assert.equal(removed.length, 2);
  const fat = Buffer.alloc(28);
  fat.writeUInt32BE(0xCAFEBABE, 0);
  fat.writeUInt32BE(1, 4);
  fat.writeUInt32BE(pack.CPU_TYPE_ARM64, 8);
  assert.equal(pack.isArm64MachO(fat), true);
  assert.throws(() => pack.freezeMacService(root, {
    exists: target => target === venv || target === requirements || fs.existsSync(target),
    execFileSync(_python, args) {
      if (args[1] !== 'PyInstaller') return;
      fs.mkdirSync(path.join(path.dirname(binary), '_internal'), {recursive: true});
      fs.writeFileSync(binary, '#!/bin/sh\nexec python3 -m noteford.service "$@"\n', {mode: 0o755});
    },
    readFile: file => fs.readFileSync(file),
    stat: executableFixtureStat,
    rm(directory) { fs.rmSync(directory, {recursive: true, force: true}); },
  }), /python3 launcher/);
  assert.equal(fs.existsSync(binary), false);
  fs.rmSync(root, {recursive: true, force: true});
});

test('Mac afterPack rejects a packaged python launcher', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-mac-app-'));
  const binary = path.join(root, 'Noteford.app', 'Contents', 'Resources', 'service', 'noteford-service');
  fs.mkdirSync(path.join(path.dirname(binary), '_internal'), {recursive: true});
  fs.writeFileSync(binary, '#!/bin/sh\nexec python3 -m noteford.service "$@"\n', {mode: 0o755});
  const io = {
    exists: fs.existsSync,
    readdir: directory => fs.readdirSync(directory),
    readFile: file => fs.readFileSync(file),
    stat: executableFixtureStat,
  };
  assert.throws(() => pack.verifyMacService(root, io), /python3 launcher/);
  fs.writeFileSync(binary, arm64Header(), {mode: 0o755});
  assert.throws(() => pack.verifyMacService(root, { ...io, stat: () => ({ mode: 0o644 }) }), /not executable/);
  assert.throws(() => pack.verifyMacService(root, io), /arm64 WeChat key hook/);
  writeMachO(path.join(path.dirname(binary), '_internal', 'mac-native', 'libwechat_key_hook.dylib'));
  assert.throws(() => pack.verifyMacService(root, io), /arm64 SILK decoder/);
  writeMachO(path.join(path.dirname(binary), '_internal', 'mac-native', 'noteford-silk'));
  assert.throws(() => pack.verifyMacService(root, io), /arm64 image decoder/);
  for (const helper of ['noteford-ffmpeg', 'noteford-ffprobe']) writeMachO(path.join(path.dirname(binary), '_internal', 'mac-native', helper));
  assert.equal(pack.verifyMacService(root, io).binary, binary);
  fs.rmSync(path.dirname(binary), {recursive: true});
  assert.throws(() => pack.verifyMacService(root, io), /self-contained/);
  fs.rmSync(root, {recursive: true, force: true});
});

test('Mac ffmpeg check compares the packaged dylib with Electron’s prebuilt copy', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-ffmpeg-'));
  const app = path.join(root, 'out', 'Noteford.app', ...pack.FFMPEG_TAIL);
  const electron = path.join(root, 'electron', 'Electron.app', ...pack.FFMPEG_TAIL);
  fs.mkdirSync(path.dirname(app), {recursive: true});
  fs.mkdirSync(path.dirname(electron), {recursive: true});
  fs.writeFileSync(app, 'prebuilt-ffmpeg');
  fs.writeFileSync(electron, 'prebuilt-ffmpeg');
  const io = {
    exists: fs.existsSync,
    readdir: directory => fs.readdirSync(directory),
    hash: file => fs.readFileSync(file).toString(),
  };
  const checked = pack.verifyMacFfmpeg(path.join(root, 'out'), path.join(root, 'electron'), io);
  assert.equal(checked.sha256, 'prebuilt-ffmpeg');
  fs.writeFileSync(app, 'different');
  assert.throws(() => pack.verifyMacFfmpeg(path.join(root, 'out'), path.join(root, 'electron'), io), /prebuilt/);
  fs.rmSync(path.dirname(app), {recursive: true});
  assert.throws(() => pack.verifyMacFfmpeg(path.join(root, 'out'), path.join(root, 'electron'), io), /prebuilt libffmpeg/);
  fs.rmSync(root, {recursive: true, force: true});
});

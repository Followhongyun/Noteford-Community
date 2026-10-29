const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const seam = require('../desktop/electron/platform-seam.cjs');
const {publishDiscovery} = require('../desktop/electron/mcp-discovery.cjs');

const desktop = path.resolve(__dirname, '../desktop');

test('Windows icons, service, data root and discovery stay on the existing paths', () => {
  const icons = {platform: 'win32', packaged: true, resourcesPath: 'C:/Program Files/Noteford/resources', desktopRoot: desktop};
  assert.equal(seam.windowIconPath(icons), path.join(icons.resourcesPath, 'noteford-book-v1.ico'));
  assert.equal(seam.trayIconPath(icons), seam.windowIconPath(icons));
  const dev = seam.serviceSpec({platform: 'win32', packaged: false, resourcesPath: '', python: 'C:/Python/python.exe', projectRoot: 'C:/src'});
  assert.deepEqual(dev, {command: 'C:/Python/python.exe', argsPrefix: ['-m', 'noteford.service'], extraEnv: {}});
  const packaged = seam.serviceSpec({platform: 'win32', packaged: true, resourcesPath: icons.resourcesPath, projectRoot: ''});
  assert.equal(packaged.command, path.join(icons.resourcesPath, 'service', 'noteford-service.exe'));
  assert.deepEqual(packaged.argsPrefix, []);
  assert.deepEqual(packaged.extraEnv, {});
  assert.equal(seam.runtimeRoot({
    packaged: true, portableLayout: false, executableRoot: 'C:/app', localAppData: 'C:/Users/a/AppData/Local',
    userData: 'C:/Users/a/AppData/Roaming/Noteford', projectRoot: 'C:/src', platform: 'win32', home: 'C:/Users/a',
  }), path.join('C:/Users/a/AppData/Local', 'Noteford Community'));
  assert.equal(seam.runtimeRoot({
    packaged: false, portableLayout: false, executableRoot: '', localAppData: '', userData: '',
    projectRoot: 'C:/src', platform: 'win32', home: 'C:/Users/a', exists: () => true,
  }), 'C:/src');
  assert.equal(seam.discoveryDirectory({
    platform: 'win32', localAppData: 'C:/Users/a/AppData/Local', home: 'C:/Users/a', appData: 'C:/Users/a/AppData/Roaming',
  }), path.join('C:/Users/a/AppData/Local', 'Noteford Community'));
  const profile = seam.hostProfile({platform: 'win32'});
  assert.equal(profile.onboarding.intro, seam.WINDOWS_INTRO);
  assert.equal(profile.onboarding.empty, seam.WINDOWS_EMPTY);
  assert.equal(profile.onboarding.permissions.pending, false);
  assert.deepEqual(seam.trayMenuTemplate('win32'), [{label: '退出', action: 'quit'}]);
  assert.equal(seam.hideOnClose('win32', true), true);
  assert.equal(seam.hideOnClose('win32', null), false);
});

test('macOS icon, python service, discovery and one-time permissions', () => {
  const home = '/Users/example';
  const resources = '/Applications/Noteford 开源版.app/Contents/Resources';
  assert.equal(seam.trayIconPath({
    platform: 'darwin', packaged: false, resourcesPath: resources, desktopRoot: desktop,
  }), path.join(desktop, 'assets', 'notefordTemplate.png'));
  assert.equal(fs.existsSync(path.join(desktop, 'assets', 'notefordTemplate.png')), true);
  assert.equal(fs.existsSync(path.join(desktop, 'assets', 'noteford.icns')), true);
  const makeImage = () => ({template: false, setTemplateImage(value) { this.template = value; }});
  const presented = seam.presentTray('darwin', {createFromPath: () => makeImage()}, 'tray.png');
  assert.equal(presented.template, true);
  assert.equal(seam.presentTray('win32', {createFromPath: () => makeImage()}, 'app.ico').template, false);
  const devService = seam.serviceSpec({
    platform: 'darwin', packaged: false, resourcesPath: resources, python: '/usr/bin/python3', projectRoot: '/src',
  });
  assert.equal(devService.command, '/usr/bin/python3');
  assert.deepEqual(devService.argsPrefix, ['-m', 'noteford.service']);
  assert.throws(() => seam.serviceSpec({
    platform: 'darwin', packaged: true, resourcesPath: resources, projectRoot: '/src', exists: () => false,
  }), /系统 python3/);
  const binary = path.join(resources, 'service', 'noteford-service');
  const launcher = Buffer.from('#!/bin/sh\nexec python3 -m noteford.service "$@"\n');
  assert.equal(seam.isPythonServiceLauncher(launcher), true);
  assert.throws(() => seam.serviceSpec({
    platform: 'darwin', packaged: true, resourcesPath: resources, projectRoot: '/src',
    exists: target => target === binary,
    readFile: () => launcher,
  }), /系统 python3/);
  const head = Buffer.alloc(32);
  head.writeUInt32LE(0xFEEDFACF, 0);
  head.writeUInt32LE(0x0100000C, 4);
  assert.equal(seam.isMachOHeader(head), true);
  const frozen = seam.serviceSpec({
    platform: 'darwin', packaged: true, resourcesPath: resources, projectRoot: '/src',
    exists: target => target === binary,
    readFile: () => head,
  });
  assert.equal(frozen.command, binary);
  assert.deepEqual(frozen.argsPrefix, []);
  assert.deepEqual(frozen.extraEnv, {});
  const main = fs.readFileSync(path.join(desktop, 'electron', 'main.cjs'), 'utf8');
  assert.match(main, /readFile: target =>/);
  assert.equal(seam.runtimeRoot({
    packaged: true, portableLayout: false, executableRoot: '', localAppData: '', userData: '',
    projectRoot: '/src', platform: 'darwin', home, exists: () => false,
  }), path.join(home, 'Noteford Community', 'Data'));
  assert.equal(seam.discoveryDirectory({platform: 'darwin', localAppData: '', home, appData: ''}), path.join(home, 'Library', 'Application Support', 'Noteford Community'));
  assert.equal(seam.shouldPublishDiscovery({smoke: false}), true);
  assert.equal(seam.shouldPublishDiscovery({smoke: true}), false);
  const pending = seam.hostProfile({platform: 'darwin', permissionsPending: true});
  assert.equal(pending.onboarding.permissions.autoRequest, false);
  assert.equal(pending.onboarding.permissions.pending, true);
  assert.equal(pending.onboarding.permissions.title, '需要权限');
  assert.match(pending.onboarding.empty, /decrypted/);
  assert.equal(seam.permissionsPending({platform: 'darwin', requiredGranted: false}), true);
  assert.equal(seam.permissionsPending({platform: 'darwin', requiredGranted: true}), false);
  assert.equal(seam.permissionsPending({platform: 'win32', requiredGranted: false}), false);
  assert.equal(seam.hideOnClose('darwin', null), true);
  assert.deepEqual(seam.trayMenuTemplate('darwin').map(item => item.label || item.type), ['打开', 'separator', '退出']);
});

test('discovery record keeps the Windows shape unless module args are present', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-discovery-args-'));
  try {
    const plain = publishDiscovery({directory, pid: 1, command: 'C:/svc/noteford-service.exe', alive: () => false});
    const record = JSON.parse(fs.readFileSync(path.join(directory, 'connection.json'), 'utf8'));
    assert.equal(record.command, 'C:/svc/noteford-service.exe');
    assert.equal('args' in record, false);
    plain();
    publishDiscovery({directory, pid: 2, command: '/usr/bin/python3', args: ['-m', 'noteford.service'], alive: () => false});
    const mac = JSON.parse(fs.readFileSync(path.join(directory, 'connection.json'), 'utf8'));
    assert.deepEqual(mac.args, ['-m', 'noteford.service']);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('packaging keeps the Windows nsis target and adds an arm64 Mac app', async () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(desktop, 'package.json'), 'utf8'));
  assert.equal(pkg.scripts['package:win'], 'node --require ./scripts/node-trust.cjs node_modules/electron-builder/cli.js --win nsis --x64 --publish never');
  assert.equal(pkg.build.win.icon, 'assets/noteford.ico');
  assert.equal(pkg.build.win.target, 'nsis');
  assert.equal(pkg.build.nsis.oneClick, false);
  assert.equal(pkg.build.extraResources.some(item => item.from === 'assets/noteford.ico' || item.to === 'noteford-book-v1.ico'), false);
  assert.equal(pkg.build.extraResources.find(item => item.to === 'service').from, '../build/service/noteford-service');
  const sharedSkill = pkg.build.extraResources.find(item => item.to === 'skills/noteford-community');
  assert.ok(sharedSkill.filter.includes('!scripts/noteford.ps1'));
  assert.deepEqual(
    pkg.build.win.extraResources.find(item => item.to === 'noteford-book-v1.ico'),
    {from: 'assets/noteford.ico', to: 'noteford-book-v1.ico'},
  );
  assert.equal(
    pkg.build.win.extraResources.find(item => item.to === 'skills/noteford-community/scripts/noteford.ps1').from,
    'mcp-plugin/skills/noteford-community/scripts/noteford.ps1',
  );
  assert.ok(pkg.build.files.indexOf('assets/**/*') < pkg.build.files.indexOf('!**/*.ico'));
  assert.ok(pkg.build.files.indexOf('mcp-plugin/**/*') < pkg.build.files.indexOf('!**/*.ps1'));
  assert.ok(pkg.build.files.indexOf('package.json') < pkg.build.files.indexOf('!**/*.ico'));
  assert.deepEqual(pkg.build.mac.files, [{from: '../build/third-party-licenses', to: 'licenses/runtime', filter: ['**/*']}]);
  assert.deepEqual(pkg.build.win.files, ['!**/node_modules/koffi/**', {filter: ['**/*.ico', '**/*.ps1']}]);
  const macSource = pkg.build.mac.extraResources.find(item => item.to === 'noteford-src/noteford');
  assert.ok(macSource.filter.includes('**/*.py'));
  assert.ok(macSource.filter.includes('!winmem.py'));
  assert.equal(pkg.scripts['package:mac'], 'node --require ./scripts/node-trust.cjs node_modules/electron-builder/cli.js --mac dmg zip --arm64 --publish never');
  assert.deepEqual(pkg.build.mac.target.map(item => item.target), ['dmg', 'zip']);
  assert.deepEqual(pkg.build.mac.target[0].arch, ['arm64']);
  assert.equal(pkg.build.mac.icon, 'assets/noteford.icns');
  const entitlements = fs.readFileSync(path.join(desktop, 'entitlements.mac.plist'), 'utf8');
  assert.match(entitlements, /com\.apple\.security\.automation\.apple-events/);
  assert.doesNotMatch(entitlements, /get-task-allow/);
  assert.match(pkg.build.mac.extendInfo.NSAppleEventsUsageDescription, /授权/);
  assert.doesNotMatch(pkg.build.mac.extendInfo.NSAppleEventsUsageDescription, /token|secret|key/i);

  // app-builder-lib writes on the test-runner channel when required in-process.
  const staged = spawnSync(process.execPath, ['-e', `
    const fs = require('node:fs');
    const os = require('node:os');
    const path = require('node:path');
    const desktop = ${JSON.stringify(desktop)};
    const {getConfig} = require(path.join(desktop, 'node_modules/app-builder-lib/out/util/config/config.js'));
    const {getMainFileMatchers, getFileMatchers, copyFiles} = require(path.join(desktop, 'node_modules/app-builder-lib/out/fileMatcher.js'));
    const {computeFileSets} = require(path.join(desktop, 'node_modules/app-builder-lib/out/util/appFileCopier.js'));
    const {expandMacro} = require(path.join(desktop, 'node_modules/app-builder-lib/out/util/macroExpander.js'));
    const appInfo = {sanitizedProductName: 'Noteford Community', productName: 'Noteford Community', channel: 'latest'};
    const macro = (pattern) => expandMacro(pattern, 'arm64', appInfo, {os: 'mac', '/*': '{,/**/*}'}, true);
    (async () => {
      const config = await getConfig(desktop);
      const info = {
        projectDir: desktop,
        buildResourcesDir: path.join(desktop, 'build'),
        isPrepackedAppAsar: false,
        config,
        debugLogger: {isEnabled: false},
        areNodeModulesHandledExternally: false,
      };
      async function asarNames(platformKey) {
        const matchers = getMainFileMatchers(desktop, path.join(os.tmpdir(), 'noteford-app'), macro, config[platformKey] || {}, {info}, path.join(os.tmpdir(), 'noteford-builder-out'), false);
        const sets = await computeFileSets(matchers, null, {info}, false);
        return new Set(sets.flatMap(set => set.files.map(file => path.relative(set.src, file).split(path.sep).join('/'))));
      }
      const macAsar = await asarNames('mac');
      const winAsar = await asarNames('win');
      const ending = (names, suffix) => [...names].filter(name => name.endsWith(suffix)).sort().join(',');
      async function stage(platformKey) {
        const destination = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-pack-'));
        const matchers = getFileMatchers(config, 'extraResources', destination, {
          macroExpander: macro,
          customBuildOptions: config[platformKey] || {},
          globalOutDir: path.join(destination, 'builder-out'),
          defaultSrc: desktop,
        });
        await copyFiles(matchers);
        return destination;
      }
      const mac = await stage('mac');
      const win = await stage('win');
      const has = (root, relative) => fs.existsSync(path.join(root, relative));
      const lines = [
        ['mac-ico', has(mac, 'noteford-book-v1.ico')],
        ['mac-ps1', has(mac, 'skills/noteford-community/scripts/noteford.ps1')],
        ['mac-sh', has(mac, 'skills/noteford-community/scripts/noteford.sh')],
        ['mac-py', has(mac, 'skills/noteford-community/scripts/noteford.py')],
        ['mac-skill', has(mac, 'skills/noteford-community/SKILL.md')],
        ['mac-winmem', has(mac, 'noteford-src/noteford/winmem.py')],
        ['mac-seam', has(mac, 'noteford-src/noteford/platform_seam.py')],
        ['mac-png', has(mac, 'noteford-mac.png')],
        ['win-ico', has(win, 'noteford-book-v1.ico')],
        ['win-ps1', has(win, 'skills/noteford-community/scripts/noteford.ps1')],
        ['win-sh', has(win, 'skills/noteford-community/scripts/noteford.sh')],
        ['win-py', has(win, 'skills/noteford-community/scripts/noteford.py')],
        ['win-winmem', has(win, 'noteford-src/noteford/winmem.py')],
        ['mac-asar-ico', ending(macAsar, '.ico')],
        ['mac-asar-ps1', ending(macAsar, '.ps1')],
        ['mac-asar-sh', macAsar.has('mcp-plugin/skills/noteford-community/scripts/noteford.sh')],
        ['mac-asar-py', macAsar.has('mcp-plugin/skills/noteford-community/scripts/noteford.py')],
        ['mac-asar-png', macAsar.has('assets/noteford-mac.png')],
        ['win-asar-ico', ending(winAsar, '.ico')],
        ['win-asar-ps1', ending(winAsar, '.ps1')],
        ['win-asar-sh', winAsar.has('mcp-plugin/skills/noteford-community/scripts/noteford.sh')],
        ['win-asar-png', winAsar.has('assets/noteford-mac.png')],
      ];
      fs.rmSync(mac, {recursive: true, force: true});
      fs.rmSync(win, {recursive: true, force: true});
      for (const [name, value] of lines) process.stdout.write(name + '=' + value + '\\n');
    })().catch((error) => {
      console.error(error);
      process.exit(1);
    });
  `], {encoding: 'utf8'});
  assert.equal(staged.status, 0, staged.stderr);
  const stagedFiles = Object.fromEntries(staged.stdout.trim().split('\n').filter(Boolean).map(line => {
    const index = line.indexOf('=');
    const raw = line.slice(index + 1);
    return [line.slice(0, index), raw === 'true' ? true : raw === 'false' ? false : raw];
  }));
  assert.equal(stagedFiles['mac-ico'], false);
  assert.equal(stagedFiles['mac-ps1'], false);
  assert.equal(stagedFiles['mac-sh'], true);
  assert.equal(stagedFiles['mac-py'], true);
  assert.equal(stagedFiles['mac-skill'], true);
  assert.equal(stagedFiles['mac-winmem'], false);
  assert.equal(stagedFiles['mac-seam'], true);
  assert.equal(stagedFiles['mac-png'], true);
  assert.equal(stagedFiles['win-ico'], true);
  assert.equal(stagedFiles['win-ps1'], true);
  assert.equal(stagedFiles['win-sh'], true);
  assert.equal(stagedFiles['win-py'], true);
  assert.equal(stagedFiles['win-winmem'], false);
  assert.equal(stagedFiles['mac-asar-ps1'], '');
  assert.equal(stagedFiles['mac-asar-sh'], true);
  assert.equal(stagedFiles['mac-asar-py'], true);
  assert.equal(stagedFiles['mac-asar-ico'], '');
  assert.equal(stagedFiles['mac-asar-png'], true);
  assert.equal(stagedFiles['win-asar-ps1'], 'mcp-plugin/skills/noteford-community/scripts/noteford.ps1');
  assert.equal(stagedFiles['win-asar-sh'], true);
  assert.equal(stagedFiles['win-asar-png'], true);
  assert.equal(stagedFiles['win-asar-ico'], 'assets/noteford.ico,assets/wechat-local.ico');
});

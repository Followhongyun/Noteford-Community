'use strict';
// Explicit local integration test. Uses only synthetic data and a distinct app
// identifier. The supplied production app is a read-only signing/runtime donor.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');
const os = require('node:os');
const { execFileSync, spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const asar = require('@electron/asar');
const { createPrivateUpdates } = require('../../distribution/private-updates.cjs');
const run = (cmd, args) => execFileSync(cmd, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
const wait = ms => new Promise(r => setTimeout(r, ms));
async function until(fn, timeout = 120000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await fn()) return; await wait(250); }
  throw Error('Timed out');
}
async function main() {
  const [donor, root, identity] = process.argv.slice(2);
  if (process.platform !== 'darwin' || !path.isAbsolute(root || '') || !identity || fs.existsSync(root))
    throw Error('Usage: node test-mac-updates.cjs <signed donor.app> <NEW absolute test dir> <Developer ID certificate SHA1>');
  // Electron derives its helper names from CFBundleName, not the app's display
  // name. Validate the donor before creating or launching a test fixture.
  const bundleName = run('plutil', ['-extract', 'CFBundleName', 'raw', '-o', '-', path.join(donor, 'Contents/Info.plist')]).trim();
  if (!bundleName || !fs.existsSync(path.join(donor, 'Contents/Frameworks', `${bundleName} Helper.app`, 'Contents/MacOS', `${bundleName} Helper`)))
    throw Error('Donor app helper does not match CFBundleName; refusing to launch a broken fixture.');
  fs.mkdirSync(root, { recursive: true });
  // Native updater helpers do not inherit terminal access to protected Documents.
  // Keep replaceable fixture apps in the OS test/temp area; evidence stays in root.
  const bundleRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-native-update-'));
  fs.writeFileSync(path.join(root, 'bundle-root.txt'), bundleRoot);
  const appId = 'com.followhongyun.noteford.updatetest' + Date.now();
  fs.writeFileSync(path.join(root, 'app-id.txt'), appId);
  const pkg = path.join(root, 'package'), feed = path.join(root, 'feed');
  fs.mkdirSync(feed);
  asar.extractAll(path.join(donor, 'Contents/Resources/app.asar'), pkg);
  for (const module of ['app-updates.cjs', 'product-account.cjs', 'product-workspace.cjs']) {
    fs.copyFileSync(path.join(__dirname, '../electron', module), path.join(pkg, 'electron', module));
  }
  // Test key is random and stays inside the test directory. No Keychain access.
  fs.writeFileSync(path.join(root, 'key'), crypto.randomBytes(32), { mode: 0o600 });
  fs.writeFileSync(path.join(pkg, 'harness.cjs'), `
const { app, autoUpdater: nativeUpdater } = require('electron');
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const { MacUpdater } = require('electron-updater');
const { createAppUpdates } = require('./electron/app-updates.cjs');
const { ProductAccount } = require('./electron/product-account.cjs');
const root = ${JSON.stringify(root)};
app.setPath('userData', path.join(root, 'profile'));
app.dock?.hide();
const log = value => fs.appendFileSync(path.join(root, 'events.jsonl'), JSON.stringify(value)+'\\n');
const key=fs.readFileSync(path.join(root,'key'));
const safeStorage={isEncryptionAvailable:()=>true,encryptString(value){
 const iv=crypto.randomBytes(12), c=crypto.createCipheriv('aes-256-gcm',key,iv);
 return Buffer.concat([iv,c.update(value),c.final(),c.getAuthTag()]);
},decryptString(value){
 const d=crypto.createDecipheriv('aes-256-gcm',key,value.subarray(0,12));
 d.setAuthTag(value.subarray(-16));return Buffer.concat([d.update(value.subarray(12,-16)),d.final()]).toString();
}};
app.whenReady().then(async()=>{
 const id='11111111-1111-4111-8111-111111111111';
 const account=new ProductAccount({directory:path.join(root,'account'),safeStorage,
 apiOrigin:'https://fixture.test',appVersion:app.getVersion(),
 activate:async()=>{ const file=path.join(root,'synthetic-database.json');
 if(!fs.existsSync(file))fs.writeFileSync(file,JSON.stringify({account:id,messages:['synthetic retained message'],following:['fixture-contact']}));
 return {epoch:'test'}; },
 fetcher:async()=>Response.json({version:'noteford-identity-v1',product:'noteford',
 account:{id,email:'fixture@example.test',displayName:'Fixture'},
 sessionToken:'nfs_'+'s'.repeat(43),revocationToken:'nfr_'+'r'.repeat(43)}) });
 if(fs.existsSync(path.join(root,'account/product-account.enc')))await account.restore();
 else await account.submit({mode:'login',email:'fixture@example.test',password:'fixture-password'});
 log({event:'launch',version:app.getVersion(),account:account.snapshot().status});
 const updater=new MacUpdater();
 Object.defineProperty(updater.app,'baseCachePath',{get:()=>path.join(root,'cache')});
 updater.logger=null;
 const controller=createAppUpdates({updater,nativeUpdater,app,authorize:()=>account.updateHeaders(),
 changed:state=>{fs.writeFileSync(path.join(root,'state.json'),JSON.stringify(state));log({event:'state',...state})},
 initialDelay:200});
 controller.start();
 app.on('before-quit',()=>{controller.stop();account.dispose();log({event:'normal-quit'})});
 setInterval(()=>{
 if(fs.existsSync(path.join(root,'check'))){fs.unlinkSync(path.join(root,'check'));void controller.check()}
 if(fs.existsSync(path.join(root,'quit'))){fs.unlinkSync(path.join(root,'quit'));app.quit()}},100);
}).catch(e=>{log({event:'failure',message:e.message});app.exit(1)});
`);
  let archive = 'Noteford-0.1.1-arm64.zip', released = true;
  const server = http.createServer((req, res) => {
    // Toggle only the synthetic feed for the second launch.
    if (!released && req.url.startsWith('/updates/noteford/macos/latest-mac.yml')) {
      res.writeHead(200, { 'content-type': 'text/yaml' });
      return res.end('version: 0.1.1\nfiles: []\n');
    }
    return handler(req, res);
  });
  const handler = createPrivateUpdates({ root: feed,
    authenticate: async req => ({ id: req.headers.authorization === 'Bearer nfs_' + 's'.repeat(43) ? 'fixture' : null, notefordAccess: true }),
    release: { version: '0.1.1', files: [archive] } });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const feedUrl = `http://127.0.0.1:${server.address().port}/updates/noteford/macos/`;
  let child;
  try {
    const builds = {};
    for (const version of ['0.1.0', '0.1.1']) {
      const bundle = path.join(bundleRoot, version, 'Noteford Update Test.app');
      fs.mkdirSync(path.dirname(bundle));
      run('ditto', [donor, bundle]);
      // The harness never uses these large resources. Only the test clone changes.
      for (const resource of ['service', 'noteford-src']) fs.rmSync(path.join(bundle, 'Contents/Resources', resource), { recursive: true, force: true });
      fs.rmSync(path.join(bundle, 'Contents/Helpers'), { recursive: true, force: true });
      const packageJson = JSON.parse(fs.readFileSync(path.join(pkg, 'package.json')));
      Object.assign(packageJson, { main: 'harness.cjs', version, name: 'noteford-update-test' });
      fs.writeFileSync(path.join(pkg, 'package.json'), JSON.stringify(packageJson));
      const archivePath = path.join(bundle, 'Contents/Resources/app.asar');
      fs.rmSync(archivePath);
      await asar.createPackage(pkg, archivePath);
      const plist = path.join(bundle, 'Contents/Info.plist');
      for (const [key, value] of Object.entries({ CFBundleIdentifier: appId, CFBundleName: bundleName,
        CFBundleDisplayName: 'Noteford Update Test', CFBundleVersion: version, CFBundleShortVersionString: version }))
        run('plutil', ['-replace', key, '-string', value, plist]);
      const hash = crypto.createHash('sha256').update(asar.getRawHeader(archivePath).headerString).digest('hex');
      run('plutil', ['-replace', 'ElectronAsarIntegrity', '-json', JSON.stringify({ 'Resources/app.asar': { algorithm: 'SHA256', hash } }), plist]);
      fs.writeFileSync(path.join(bundle, 'Contents/Resources/app-update.yml'),
        `provider: generic\nurl: ${feedUrl}\nupdaterCacheDirName: noteford-update-test\n`);
      run('codesign', ['--force', '--sign', identity, '--timestamp', '--options', 'runtime',
        '--entitlements', path.join(__dirname, '../entitlements.mac.plist'), bundle]);
      run('codesign', ['--verify', '--deep', '--strict', bundle]);
      builds[version] = bundle;
      process.stdout.write(JSON.stringify({ event: 'signed', version, bundle }) + '\n');
    }
    run('ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', builds['0.1.1'], path.join(feed, archive)]);
    const bytes = fs.readFileSync(path.join(feed, archive));
    const manifest = checksum => `version: 0.1.1\nfiles:\n  - url: ${archive}\n    sha512: ${checksum}\n    size: ${bytes.length}\n`;
    fs.writeFileSync(path.join(feed, 'latest-mac.yml'), manifest(Buffer.alloc(64).toString('base64')));
    assert.equal((await fetch(feedUrl + archive)).status, 403);
    const launch = () => spawn(path.join(builds['0.1.0'], 'Contents/MacOS/Noteford'), [], {
      stdio: ['ignore', fs.openSync(path.join(root, 'stdout.log'), 'a'), fs.openSync(path.join(root, 'stderr.log'), 'a')],
      env: { ...process.env, ELECTRON_RUN_AS_NODE: '' } });
    child = launch();
    await until(() => {
      if (child.exitCode !== null || child.signalCode) throw Error('Fixture exited during corrupt-download test');
      return fs.existsSync(path.join(root, 'state.json')) && JSON.parse(fs.readFileSync(path.join(root, 'state.json'))).status === 'error';
    });
    assert.equal(run('plutil', ['-extract', 'CFBundleShortVersionString', 'raw', '-o', '-', path.join(builds['0.1.0'], 'Contents/Info.plist')]).trim(), '0.1.0');
    process.stdout.write('Corrupt download refused; original signed app remains running. Retrying valid release.\n');
    fs.writeFileSync(path.join(feed, 'latest-mac.yml'), manifest(crypto.createHash('sha512').update(bytes).digest('base64')));
    fs.writeFileSync(path.join(root, 'check'), '');
    await until(() => {
      if (child.exitCode !== null || child.signalCode) throw Error('Fixture app exited before update staged');
      return fs.existsSync(path.join(root, 'state.json')) && JSON.parse(fs.readFileSync(path.join(root, 'state.json'))).status === 'ready';
    });
    assert.equal(child.exitCode, null);
    assert.equal(run('plutil', ['-extract', 'CFBundleShortVersionString', 'raw', '-o', '-', path.join(builds['0.1.0'], 'Contents/Info.plist')]).trim(), '0.1.0');
    const preserved = ['account/product-account.enc', 'synthetic-database.json'];
    const hashes = () => preserved.map(file => crypto.createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex'));
    const before = hashes();
    process.stdout.write('Update staged while original app is still running; requesting ordinary quit.\n');
    fs.writeFileSync(path.join(root, 'quit'), '');
    await until(() => child.exitCode !== null);
    await until(() => {
      try { return run('plutil', ['-extract', 'CFBundleShortVersionString', 'raw', '-o', '-', path.join(builds['0.1.0'], 'Contents/Info.plist')]).trim() === '0.1.1'; }
      catch { return false; }
    });
    assert.deepEqual(hashes(), before);
    run('codesign', ['--verify', '--deep', '--strict', builds['0.1.0']]);
    await wait(2000);
    const events = () => fs.readFileSync(path.join(root, 'events.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(events().filter(e => e.event === 'launch').length, 1, 'must not auto-relaunch');
    released = false; child = launch();
    await until(() => events().filter(e => e.event === 'launch').length === 2);
    const launches = events().filter(e => e.event === 'launch');
    assert.deepEqual(launches.map(e => [e.version, e.account]), [['0.1.0', 'signed-in'], ['0.1.1', 'signed-in']]);
    assert.equal(hashes()[1], before[1]);
    fs.writeFileSync(path.join(root, 'quit'), '');
    await until(() => child.exitCode !== null);
    fs.writeFileSync(path.join(root, 'result.json'), JSON.stringify({ passed: true, appId, versionFrom: '0.1.0', versionTo: '0.1.1',
      backgroundDownload: true, normalQuitInstall: true, automaticRelaunch: false, encryptedAccountPreserved: true,
      databasePreserved: true, signedInAfterManualLaunch: true, signatureVerified: true, corruptDownloadRejected: true }, null, 2));
    process.stdout.write('PASS: signed native update, normal quit, no restart, remembered account and data retained.\n');
  } finally {
    try {
      if (child && child.exitCode === null && !child.signalCode) {
        child.kill();
        try { await until(() => child.exitCode !== null || child.signalCode, 10000); }
        catch { child.kill('SIGKILL'); await until(() => child.exitCode !== null || child.signalCode, 10000); }
      }
    } finally {
      server.closeAllConnections(); server.close();
      // Keep diagnostic evidence, not launchable test apps that macOS can reopen.
      // Never touch production apps, accounts or updater caches.
      try { run('launchctl', ['remove', appId + '.ShipIt']); } catch {}
      for (const relative of [`Caches/${appId}.ShipIt`, `Caches/${appId}`,
        `Preferences/${appId}.plist`, `Saved Application State/${appId}.savedState`]) {
        fs.rmSync(path.join(os.homedir(), 'Library', relative), { recursive: true, force: true });
      }
      fs.rmSync(bundleRoot, { recursive: true, force: true });
    }
  }
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });

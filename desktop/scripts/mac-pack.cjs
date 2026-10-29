'use strict';

const {execFileSync} = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const FFMPEG_TAIL = [
  'Contents', 'Frameworks', 'Electron Framework.framework', 'Versions', 'A', 'Libraries', 'libffmpeg.dylib',
];

const WINDOWS_ONLY_REQUIREMENTS = ['pefile', 'pywin32', 'pywin32-ctypes'];
const SERVICE_IMPORTS = 'import cryptography, pydantic, uvicorn, mcp, psutil, zstandard, truststore';
const CPU_TYPE_ARM64 = 0x0100000C;
const MH_MAGIC_64 = 0xFEEDFACF;

function resolvePackPython(root, platform) {
  if (platform === 'win32') return path.join(root, '.venv-build', 'Scripts', 'python.exe');
  return path.join(root, '.venv-build', 'bin', 'python3');
}

function macRequirementsPath(root) {
  return path.join(root, 'requirements-build-mac.txt');
}

function pyinstallerArgs(root) {
  return [
    '-m', 'PyInstaller',
    '--noconfirm',
    '--onedir',
    '--name', 'noteford-service',
    '--target-architecture', 'arm64',
    '--collect-submodules', 'noteford',
    '--paths', path.join(root, 'src'),
    '--add-binary', `${path.join(root, 'build', 'mac-native', 'libwechat_key_hook.dylib')}:mac-native`,
    '--add-binary', `${path.join(root, 'build', 'mac-native', 'noteford-silk')}:mac-native`,
    '--add-binary', `${path.join(root, 'build', 'mac-native', 'noteford-ffmpeg')}:mac-native`,
    '--add-binary', `${path.join(root, 'build', 'mac-native', 'noteford-ffprobe')}:mac-native`,
    '--distpath', path.join(root, 'build', 'service'),
    '--workpath', path.join(root, 'build', 'pyinstaller-work'),
    '--specpath', path.join(root, 'build'),
    path.join(root, 'packaging', 'service-entry.py'),
  ];
}

function serviceBinary(root) {
  return path.join(root, 'build', 'service', 'noteford-service', 'noteford-service');
}

function headerBuffer(value) {
  if (Buffer.isBuffer(value)) return value;
  if (value instanceof Uint8Array) return Buffer.from(value);
  return Buffer.from(String(value || ''), 'utf8');
}

function isPythonServiceLauncher(value) {
  const text = headerBuffer(value).subarray(0, 512).toString('utf8');
  return text.startsWith('#!') || text.includes('python3 -m noteford.service') || text.includes('python -m noteford.service');
}

function isArm64MachO(value) {
  const buf = headerBuffer(value);
  if (buf.length < 8) return false;
  if (buf.readUInt32LE(0) === MH_MAGIC_64 && buf.readUInt32LE(4) === CPU_TYPE_ARM64) return true;
  if (buf[0] !== 0xCA || buf[1] !== 0xFE || buf[2] !== 0xBA || buf[3] !== 0xBE || buf.length < 12) return false;
  const count = buf.readUInt32BE(4);
  for (let index = 0; index < count; index += 1) {
    const offset = 8 + index * 20;
    if (offset + 4 > buf.length) return false;
    if (buf.readUInt32BE(offset) === CPU_TYPE_ARM64) return true;
  }
  return false;
}

function assertFrozenMacService(binary, readFile, stat, exists) {
  const head = headerBuffer(readFile(binary)).subarray(0, 512);
  if (isPythonServiceLauncher(head) || !isArm64MachO(head)) {
    throw new Error('Mac packaging did not produce a self-contained arm64 noteford-service. Refusing to ship a python3 launcher.');
  }
  if ((stat(binary).mode & 0o111) === 0) {
    throw new Error('Frozen noteford-service is not executable.');
  }
  const internal = path.join(path.dirname(binary), '_internal');
  if (!exists(internal)) {
    throw new Error('Frozen noteford-service is missing its _internal runtime.');
  }
  const hook = path.join(internal, 'mac-native', 'libwechat_key_hook.dylib');
  if (!exists(hook) || !isArm64MachO(readFile(hook))) {
    throw new Error('Frozen noteford-service is missing its arm64 WeChat key hook.');
  }
  const silk = path.join(internal, 'mac-native', 'noteford-silk');
  if (!exists(silk) || !isArm64MachO(readFile(silk)) || (stat(silk).mode & 0o111) === 0) {
    throw new Error('Frozen noteford-service is missing its executable arm64 SILK decoder.');
  }
  for (const name of ['noteford-ffmpeg', 'noteford-ffprobe']) {
    const helper = path.join(internal, 'mac-native', name);
    if (!exists(helper) || !isArm64MachO(readFile(helper)) || (stat(helper).mode & 0o111) === 0) {
      throw new Error('Frozen noteford-service is missing its bundled arm64 image decoder.');
    }
  }
  return binary;
}

function freezeMacService(root, io) {
  const requirements = macRequirementsPath(root);
  if (!io.exists(requirements)) throw new Error('Missing requirements-build-mac.txt');
  const python = resolvePackPython(root, 'darwin');
  if (!io.exists(python)) {
    io.execFileSync('python3', ['-m', 'venv', path.join(root, '.venv-build')], {cwd: root, stdio: 'inherit'});
    if (!io.exists(python)) {
      throw new Error('Mac packaging could not create .venv-build. The build machine needs python3; the installed app does not.');
    }
  }
  io.execFileSync(python, ['-I', path.join(root, 'Scripts', 'bootstrap-build-pip.py')], {
    cwd: root, stdio: 'inherit',
  });
  io.execFileSync(python, ['-m', 'pip', 'install', '--require-virtualenv', '--disable-pip-version-check', '-r', requirements], {
    cwd: root, stdio: 'inherit',
  });
  try {
    io.execFileSync(python, ['-c', SERVICE_IMPORTS], {cwd: root, stdio: 'inherit'});
  } catch (error) {
    throw new Error('Mac service freeze is missing Python dependencies from requirements-build-mac.txt. ' + (error && error.message ? error.message : error));
  }
  const directory = path.dirname(serviceBinary(root));
  io.rm(directory);
  const env = {...process.env, PYTHONPATH: [path.join(root, 'src'), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)};
  // Compile on the build machine. Installed apps must not require a checkout
  // or developer tools just to connect WeChat for the first time.
  io.execFileSync(python, ['-c', 'from pathlib import Path; from noteford.mac_interpose import build_hook; build_hook(Path("build/mac-native"))'], {
    cwd: root, stdio: 'inherit', env,
  });
  io.execFileSync(python, ['-c', 'from pathlib import Path; from noteford.mac_silk_build import build_silk; build_silk(Path("build/mac-native"))'], {
    cwd: root, stdio: 'inherit', env,
  });
  io.execFileSync(python, ['-c', 'from pathlib import Path; from noteford.mac_image_build import build_images; build_images(Path("build/mac-native"))'], {
    cwd: root, stdio: 'inherit', env: {...process.env, PYTHONPATH: path.join(root, 'src')},
  });
  try {
    io.execFileSync(python, pyinstallerArgs(root), {cwd: root, stdio: 'inherit', env});
  } catch (error) {
    io.rm(directory);
    throw new Error('PyInstaller failed to freeze noteford-service. Packaging stops instead of shipping a python3 launcher. ' + (error && error.message ? error.message : error));
  }
  const binary = serviceBinary(root);
  if (!io.exists(binary)) {
    throw new Error('PyInstaller did not write build/service/noteford-service/noteford-service');
  }
  try {
    return {python, binary: assertFrozenMacService(binary, io.readFile, io.stat, io.exists)};
  } catch (error) {
    io.rm(directory);
    throw error;
  }
}

function prepareMacPack(root, io = {}) {
  if (!io.execFileSync) require('./node-trust.cjs');
  if (!io.execFileSync) verifyAutomationBridge(path.join(root, 'desktop', 'node_modules', 'koffi'));
  const bound = {
    exists: io.exists || fs.existsSync,
    execFileSync: io.execFileSync || execFileSync,
    readFile: io.readFile || (file => fs.readFileSync(file)),
    stat: io.stat || (file => fs.statSync(file)),
    rm: io.rm || (directory => fs.rmSync(directory, {recursive: true, force: true})),
  };
  const frozen = freezeMacService(root, bound);
  const licenses = path.join(root, 'build', 'third-party-licenses');
  fs.mkdirSync(licenses, {recursive: true});
  const note = {
    platform: 'darwin',
    ffmpeg: 'electron-prebuilt-libffmpeg.dylib',
    source_archive: null,
    image_decoder: 'mac-image-decoder/ffmpeg-8.1.3.tar.xz (LGPL, network disabled)',
    service: 'pyinstaller-onedir-arm64',
    note: 'Uses the libffmpeg.dylib already inside the installed Electron app. The local service is a frozen arm64 binary and does not call system python3.',
  };
  fs.writeFileSync(path.join(licenses, 'mac-ffmpeg.json'), JSON.stringify(note, null, 2) + '\n');
  return frozen;
}

function prebuiltFfmpeg(root, exists, readdir) {
  const direct = path.join(root, ...FFMPEG_TAIL);
  if (exists(direct)) return direct;
  let names = [];
  try {
    names = readdir(root);
  } catch {
    return null;
  }
  for (const name of names) {
    if (!name.endsWith('.app')) continue;
    const candidate = path.join(root, name, ...FFMPEG_TAIL);
    if (exists(candidate)) return candidate;
  }
  return null;
}

function macAppBundle(appOutDir, exists, readdir) {
  const direct = path.join(appOutDir, 'Noteford.app');
  if (exists(direct)) return direct;
  let names = [];
  try {
    names = readdir(appOutDir);
  } catch {
    return null;
  }
  const name = names.find(item => item.endsWith('.app'));
  return name ? path.join(appOutDir, name) : null;
}

function verifyMacFfmpeg(appOutDir, electronDist, io) {
  const packed = prebuiltFfmpeg(appOutDir, io.exists, io.readdir);
  const bundled = prebuiltFfmpeg(electronDist, io.exists, io.readdir);
  if (!packed || !bundled) {
    throw new Error('Mac packaging needs the prebuilt libffmpeg.dylib shipped with Electron. The Windows ffmpeg source archive is not used.');
  }
  if (io.hash(packed) !== io.hash(bundled)) {
    throw new Error('Packaged libffmpeg.dylib differs from the Electron prebuilt binary');
  }
  return {packed, sha256: io.hash(packed)};
}

function verifyMacService(appOutDir, io) {
  const bundle = macAppBundle(appOutDir, io.exists, io.readdir);
  if (!bundle) throw new Error('Mac packaging did not produce a Noteford.app bundle.');
  const binary = path.join(bundle, 'Contents', 'Resources', 'service', 'noteford-service');
  if (!io.exists(binary)) {
    throw new Error('Mac packaging did not produce a self-contained arm64 noteford-service. Refusing to ship a python3 launcher.');
  }
  assertFrozenMacService(binary, io.readFile, io.stat, io.exists);
  return {binary};
}

function codeSignArguments(target, { identity, identifier, entitlements } = {}) {
  if (!identity) throw new Error('A signing identity is required.');
  const args = ['--force', '--sign', identity];
  if (identity !== '-') args.push('--timestamp=http://timestamp.apple.com/ts01');
  if (identifier) args.push('--identifier', identifier);
  if (entitlements) args.push('--entitlements', entitlements);
  args.push('--options', 'runtime', target);
  return args;
}

function adhocSignArguments(target, options = {}) {
  return codeSignArguments(target, { ...options, identity: '-' });
}

function signTarget(target, options, run = execFileSync) {
  const args = codeSignArguments(target, options);
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      return run('/usr/bin/codesign', args, { stdio: 'pipe' });
    } catch (error) {
      // Apple's TSA can fail transiently. Retry the identical signed request;
      // never omit the timestamp, change identity or ignore a signature error.
      if (options.identity === '-' || attempt === 4
          || !String(error.stderr || '').includes('The timestamp service is not available.')) throw error;
    }
  }
}

function resolveDeveloperIdentity({ teamId, env = process.env, execFileSync: run = execFileSync } = {}) {
  if (!/^[A-Z0-9]{10}$/.test(teamId || '')) throw new Error('The release signing Team ID must be configured.');
  const listing = run('/usr/bin/security', ['find-identity', '-v', '-p', 'codesigning'], { encoding: 'utf8' });
  const identities = [...String(listing).matchAll(/\b([A-Fa-f0-9]{40}) "(Developer ID Application: [^"\n]+)"/g)]
    .map(match => ({ hash: match[1], name: match[2] }))
    .filter(item => item.name.endsWith(`(${teamId})`))
    .filter(item => !env.CSC_NAME || env.CSC_NAME === item.name || env.CSC_NAME.toUpperCase() === item.hash.toUpperCase());
  if (identities.length !== 1) {
    throw new Error('Mac release packaging requires one valid Developer ID Application identity for team ' + teamId
      + '. Use CSC_NAME to select a matching certificate. Ad-hoc fallback is disabled.');
  }
  return identities[0].hash;
}

function verifySignatureArguments(target) {
  return ['--verify', '--deep', '--strict', target];
}

function isMachOBuffer(value) {
  const buf = headerBuffer(value);
  if (buf.length < 4) return false;
  const little = buf.readUInt32LE(0);
  const big = buf.readUInt32BE(0);
  return little === 0xFEEDFACF || little === 0xFEEDFACE || little === 0xCFFAEDFE || little === 0xCEFAEDFE
    || big === 0xCAFEBABE || big === 0xBEBAFECA;
}

function isBundleMainExecutable(file) {
  const parts = String(file).split(path.sep);
  const macos = parts.lastIndexOf('MacOS');
  if (macos > 0 && parts[macos - 1] === 'Contents') return true;
  const framework = parts.findIndex(part => part.endsWith('.framework'));
  if (framework >= 0 && parts[parts.length - 1] === parts[framework].slice(0, -'.framework'.length)) return true;
  return false;
}

function walkBundleFiles(root, io) {
  const files = [];
  function visit(directory) {
    const entries = io.readdir(directory, { withFileTypes: true });
    for (const entry of entries) {
      const file = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) visit(file);
      else files.push(file);
    }
  }
  visit(root);
  return files;
}

const bundleIo = {
  readdir: (directory, options) => fs.readdirSync(directory, options),
  readFile: file => {
    const fd = fs.openSync(file, 'r');
    try {
      const buf = Buffer.alloc(16);
      const size = fs.readSync(fd, buf, 0, 16, 0);
      return buf.subarray(0, size);
    } finally {
      fs.closeSync(fd);
    }
  },
};

function readFileHead(file, io) {
  return headerBuffer(io.readFile(file)).subarray(0, 16);
}

function adhocSignPlan(appPath, { identifier = 'com.followhongyun.noteford', entitlements, io = bundleIo } = {}) {
  if (!appPath) throw new Error('Mac packaging did not produce a Noteford.app bundle to sign.');
  const files = walkBundleFiles(appPath, io);
  const binaries = files
    .filter(file => !isBundleMainExecutable(file) && isMachOBuffer(readFileHead(file, io)))
    .sort((left, right) => right.split(path.sep).length - left.split(path.sep).length || (left < right ? -1 : 1))
    .map(file => {
      const head = readFileHead(file, io);
      // Standalone arm64 executables (the frozen Python service, crashpad)
      // also need the runtime policy when loading bundled libraries.
      const executable = head.length >= 16 && head.readUInt32LE(0) === MH_MAGIC_64 && head.readUInt32LE(12) === 2;
      return { kind: 'binary', target: file, entitlements: executable ? entitlements : undefined };
    });
  const bundles = [];
  function visitBundles(directory) {
    const entries = io.readdir(directory, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
      const file = path.join(directory, entry.name);
      if (entry.name.endsWith('.framework')) bundles.push({ kind: 'framework', target: file });
      else if (entry.name.endsWith('.app')) bundles.push({ kind: 'helper', target: file });
      visitBundles(file);
    }
  }
  visitBundles(appPath);
  bundles.sort((left, right) => {
    const leftInsideRight = left.target.startsWith(right.target + path.sep);
    const rightInsideLeft = right.target.startsWith(left.target + path.sep);
    if (leftInsideRight) return -1;
    if (rightInsideLeft) return 1;
    if (left.kind !== right.kind) return left.kind === 'framework' ? -1 : 1;
    return left.target < right.target ? -1 : 1;
  });
  const nested = bundles.map(item => ({
    kind: item.kind,
    target: item.target,
    entitlements: item.kind === 'helper' ? entitlements : undefined,
  }));
  return [
    ...binaries,
    ...nested,
    { kind: 'app', target: appPath, identifier, entitlements },
  ];
}

function signApp(appPath, {
  identity,
  execFileSync: run = execFileSync,
  identifier = 'com.followhongyun.noteford',
  entitlements = path.join(__dirname, '..', 'entitlements.mac.plist'),
  io = bundleIo,
} = {}) {
  const plan = adhocSignPlan(appPath, { identifier, entitlements, io });
  for (const item of plan) {
    signTarget(item.target, { ...item, identity }, run);
  }
  try {
    run('/usr/bin/codesign', verifySignatureArguments(appPath), { stdio: 'pipe' });
  } catch (error) {
    const detail = error && error.stderr ? error.stderr.toString() : (error && error.message ? error.message : '');
    throw new Error('Mac packaging codesign --verify --deep --strict failed. ' + detail);
  }
  return { app: appPath, identifier, signed: plan.map(item => item.target) };
}

// Explicitly limited to synthetic fixtures / local development. Release hooks
// must use signMacApp so a missing certificate cannot silently change trust.
function resignAdhocApp(appPath, options = {}) {
  return signApp(appPath, { ...options, identity: '-' });
}

function signMacApp(appPath, options = {}) {
  const run = options.execFileSync || execFileSync;
  const identity = resolveDeveloperIdentity(options);
  const result = signApp(appPath, { ...options, identity });
  const requirement = run('/usr/bin/codesign', ['-d', '-r-', appPath], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  // codesign's default Developer ID requirement binds both the publisher and
  // bundle identifier. Never replace it with an identifier-only requirement.
  const dr = String(requirement);
  if (!dr.includes('anchor apple generic') || !dr.includes('certificate leaf[subject.OU] = ' + options.teamId)
      || !dr.includes('identifier "' + result.identifier + '"') || /designated => cdhash/.test(dr)) {
    throw new Error('Mac release signature does not have the expected stable Developer ID requirement.');
  }
  return result;
}

function verifyAutomationBridge(modulePath) {
  try {
    // Fail packaging if optional-dependency installation silently skipped Koffi,
    // or its native binary / Apple Event ABI cannot load. This never prompts.
    require('../electron/automation-check.cjs').loadCore(require.resolve(modulePath));
  } catch (error) {
    throw new Error('Mac automation bridge is missing or invalid. Install optional dependencies before packaging. ' + error.message);
  }
}

function verifyMacNotices(app, source) {
  const {extractFile, statFile} = require('@electron/asar');
  const resources = path.join(app, 'Contents', 'Resources');
  if (fs.existsSync(path.join(resources, 'third-party-licenses'))) {
    throw new Error('Mac notices must be inside app.asar, not a separate resource directory.');
  }
  const files = walkBundleFiles(source, {readdir: fs.readdirSync});
  if (!files.length) throw new Error('Mac notice collection is empty.');
  const archive = path.join(resources, 'app.asar');
  for (const file of files) {
    const name = path.posix.join('licenses/runtime', path.relative(source, file).split(path.sep).join('/'));
    if (statFile(archive, name).unpacked || !extractFile(archive, name).equals(fs.readFileSync(file))) {
      throw new Error('Mac bundled notice missing, unpacked or changed: ' + name);
    }
  }
  return {noticeFiles: files.length};
}

function verifyMacPack(context) {
  const desktop = path.resolve(__dirname, '..');
  const hash = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const io = {
    exists: fs.existsSync,
    readdir: directory => fs.readdirSync(directory),
    readFile: file => {
      const fd = fs.openSync(file, 'r');
      try {
        const buf = Buffer.alloc(512);
        const size = fs.readSync(fd, buf, 0, 512, 0);
        return buf.subarray(0, size);
      } finally {
        fs.closeSync(fd);
      }
    },
    stat: file => fs.statSync(file),
    hash,
  };
  const ffmpeg = verifyMacFfmpeg(context.appOutDir, path.join(desktop, 'node_modules', 'electron', 'dist'), io);
  const service = verifyMacService(context.appOutDir, io);
  const bundle = macAppBundle(context.appOutDir, io.exists, io.readdir);
  const notices = verifyMacNotices(bundle, path.resolve(desktop, '../build/third-party-licenses'));
  verifyAutomationBridge(path.join(bundle, 'Contents', 'Resources', 'app.asar.unpacked', 'node_modules', 'koffi'));
  return {...ffmpeg, ...notices, service: service.binary};
}

module.exports = {
  CPU_TYPE_ARM64,
  FFMPEG_TAIL,
  SERVICE_IMPORTS,
  WINDOWS_ONLY_REQUIREMENTS,
  assertFrozenMacService,
  freezeMacService,
  isArm64MachO,
  isPythonServiceLauncher,
  adhocSignArguments,
  codeSignArguments,
  signTarget,
  resolveDeveloperIdentity,
  adhocSignPlan,
  isBundleMainExecutable,
  isMachOBuffer,
  macAppBundle,
  prepareMacPack,
  resignAdhocApp,
  signMacApp,
  prebuiltFfmpeg,
  pyinstallerArgs,
  resolvePackPython,
  serviceBinary,
  verifyMacFfmpeg,
  verifyAutomationBridge,
  verifyMacPack,
  verifyMacNotices,
  verifyMacService,
  verifySignatureArguments,
};

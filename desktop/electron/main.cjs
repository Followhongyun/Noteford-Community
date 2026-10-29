'use strict';

const { app, BrowserWindow, ipcMain, Menu, protocol, net, dialog, shell, Notification, Tray, clipboard, powerMonitor, nativeImage } = require('electron');
const mode = require('./runtime-mode.cjs').runtimeMode({packaged:app.isPackaged,argv:process.argv});
const path = require('node:path');
const macAccountScope = process.platform === 'darwin' ? new (require('node:async_hooks').AsyncLocalStorage)() : null;
const { pathToFileURL } = require('node:url');
const fs = require('node:fs');
const crypto = require('node:crypto');
const { Readable } = require('node:stream');
const { spawn } = require('node:child_process');
const os = require('node:os');
const { windowChrome } = require('./window-chrome.cjs');
const seam = require('./platform-seam.cjs');

const desktopRoot = path.resolve(__dirname, '..');
const projectRoot = path.resolve(desktopRoot, '..');
const productName = 'Noteford 开源版';
app.setName(productName);
app.setPath('userData', path.join(app.getPath('appData'), 'Noteford Community'));
const appUserModelId = 'com.followhongyun.noteford.community';
const executableRoot = path.dirname(process.execPath);
const singleFolderRoot = path.dirname(executableRoot);
const portableLayout = app.isPackaged && (
  fs.existsSync(path.join(executableRoot, '.portable-layout')) ||
  (path.basename(executableRoot).toLowerCase() === 'app' &&
   fs.existsSync(path.join(singleFolderRoot, 'config.local.json')))
);
const isLocalLive = mode.localLive;
if (isLocalLive && (!process.env.NOTEFORD_DATA_ROOT || !path.isAbsolute(process.env.NOTEFORD_DATA_ROOT) || !fs.existsSync(process.platform === 'darwin' ? process.env.NOTEFORD_DATA_ROOT : path.join(process.env.NOTEFORD_DATA_ROOT, 'config.local.json')))) {
  throw new Error('Local live mode requires an existing absolute data root.');
}
const installationRoot = isLocalLive ? path.resolve(process.env.NOTEFORD_DATA_ROOT) : seam.runtimeRoot({
  packaged: app.isPackaged,
  portableLayout,
  executableRoot,
  localAppData: process.env.LOCALAPPDATA,
  userData: app.getPath('userData'),
  projectRoot,
  platform: process.platform,
  home: os.homedir(),
  exists: target => fs.existsSync(target),
});
const { LocalWorkspace } = require('./local-workspace.cjs');
let workspaces;
let runtimeRoot = null;
const workspaceChannels = new Set();
let workspaceCleanup = [];
let stoppingWorkspace = Promise.resolve();

function scopedIpc(scope) {
  return { handle(channel, handler) {
    workspaceChannels.add(channel);
    ipcMain.handle(channel, async (event, envelope, ...args) => {
      assertMainRenderer(event);
      workspaces.assert(scope);
      if (envelope?.workspaceEpoch !== scope.epoch) throw Error('账号已切换，请重新打开页面。');
      return workspaces.run(scope, async () => {
        const result = await handler(event, ...args);
        workspaces.assert(scope);
        return result;
      });
    });
  } };
}

function deactivateWorkspace() {
  const hadWorkspace = !!workspaces.active;
  workspaces.lock();
  runtimeRoot = null;
  serviceRecovery.stop();
  unpublishMcpDiscovery?.(); unpublishMcpDiscovery = null;
  localPairingWatch?.(); localPairingWatch = null;
  for (const cleanup of workspaceCleanup.splice(0)) cleanup();
  for (const channel of workspaceChannels) ipcMain.removeHandler(channel);
  workspaceChannels.clear();
  mediaMap.clear(); remoteMap.clear();
  const child = serviceProcess;
  const remoteStopped = remoteMcp?.stop(); remoteMcp = null;
  const stopped = child ? new Promise((resolve, reject) => {
    if (child.exitCode !== null || child.signalCode) return resolve();
    child.once('exit', resolve);
    child.kill();
    const force = setTimeout(() => child.kill('SIGKILL'), 3000);
    const timeout = setTimeout(() => reject(Error('workspace_service_still_running')), 6000);
    child.once('exit', () => { clearTimeout(force); clearTimeout(timeout); });
  }) : Promise.resolve();
  stoppingWorkspace = Promise.all([stoppingWorkspace, stopped, remoteStopped]).then(async () => {
    if (hadWorkspace && mainWindow && !mainWindow.isDestroyed() && !quitting) {
      await mainWindow.webContents.session.clearCache();
      // Destroy queued renderer work from the previous identity before a new login.
      if (useLiveRenderer) await mainWindow.loadURL('http://127.0.0.1:5187');
      else await mainWindow.loadFile(path.join(desktopRoot, 'dist', 'index.html'));
    }
  });
  return stoppingWorkspace;
}

async function activateWorkspace(account) {
  await stoppingWorkspace;
  const scope = workspaces.activate(account);
  runtimeRoot = scope.root;
  await workspaces.run(scope, () => startService());
  workspaces.assert(scope);
  registerIpc(scope);
  publishWorkspaceDiscovery();
  if (!isSmoke) serviceRecovery.start();
  return { epoch: scope.epoch, legacy: scope.workspace === 'legacy' };
}

// Windows Shell cannot resolve icons inside app.asar for taskbar relaunch metadata.
const iconPaths = {
  platform: process.platform,
  packaged: app.isPackaged,
  resourcesPath: process.resourcesPath,
  desktopRoot,
};
const appIcon = seam.windowIconPath(iconPaths);
const isDev = mode.development;
const useLiveRenderer = isDev || isLocalLive;
const isSmoke = process.argv.includes('--smoke');
let mainWindow = null;
let tray = null;
let quitting = false;
let localPairingWatch = null;
let unpublishMcpDiscovery = null;
function assertMainRenderer(event) {
  const expectedUrl = useLiveRenderer
    ? 'http://127.0.0.1:5187/'
    : pathToFileURL(path.join(desktopRoot, 'dist', 'index.html')).toString();
  if (!mainWindow || event.sender !== mainWindow.webContents || event.senderFrame !== event.sender.mainFrame || event.senderFrame.url !== expectedUrl) {
    throw new Error('Blocked IPC sender');
  }
}

protocol.registerSchemesAsPrivileged([
  {
    scheme: 'noteford-media',
    privileges: {
      standard: true,
      secure: true,
      stream: true,
      supportFetchAPI: true,
      corsEnabled: true,
    },
  },
  {
    scheme: 'noteford-remote',
    privileges: {
      standard: true,
      secure: true,
      stream: true,
      supportFetchAPI: true,
      corsEnabled: true,
    },
  },
]);

let serviceProcess = null;
let serviceInfo = null;
let serviceStartup = null;
let apiToken = null;
const serviceRecovery = require('./service-recovery.cjs').createServiceRecovery({
  start: () => quitting || !workspaces.active ? Promise.resolve() : startService(),
  current: () => serviceProcess ? { pid: serviceProcess.pid, ready: !!serviceInfo, port: serviceInfo?.port } : null,
  health: async child => {
    const response = await fetch(`http://127.0.0.1:${child.port}/api/v1/health`, { signal: AbortSignal.timeout(5000) });
    if (!response.ok || (await response.json()).ok !== true) throw new Error('service_unavailable');
  },
  terminate: child => { if (!quitting && serviceProcess?.pid === child.pid) serviceProcess.kill(); },
});
const mediaMap = new Map();
const remoteMap = new Map();

function projectPath(...parts) {
  return path.join(workspaces.assert().root, ...parts);
}

const runtimeProfile = path.join(installationRoot,
  'data',
  'runtime',
  isSmoke ? 'electron-smoke' : 'electron'
);
fs.mkdirSync(runtimeProfile, { recursive: true });
app.setPath('userData', runtimeProfile);
app.setAppUserModelId(appUserModelId);

function ensureRuntimeConfig() {
  if (!app.isPackaged) return;
  const configFile = projectPath('config.local.json');
  if (fs.existsSync(configFile)) return;
  fs.mkdirSync(runtimeRoot, { recursive: true });
  const config = {
    project_root: runtimeRoot,
    wechat_roots: [],
    library_registry_path: projectPath('data', 'library_registry.json'),
    allow_memory_rescan: true,
    auto_refresh_interval_seconds: 300,
  };
  const temporary = configFile + '.' + crypto.randomUUID() + '.tmp';
  try {
    fs.writeFileSync(temporary, JSON.stringify(config, null, 2), { flag: 'wx' });
    fs.renameSync(temporary, configFile);
  } finally {
    try { fs.unlinkSync(temporary); } catch {}
  }
}

function currentServiceSpec() {
  return seam.serviceSpec({
    platform: process.platform,
    packaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    python: process.env.NOTEFORD_PYTHON,
    projectRoot,
    exists: target => fs.existsSync(target),
    readFile: target => {
      const fd = fs.openSync(target, 'r');
      try {
        const buf = Buffer.alloc(512);
        const size = fs.readSync(fd, buf, 0, 512, 0);
        return buf.subarray(0, size);
      } finally {
        fs.closeSync(fd);
      }
    },
  });
}

function startService() {
  let scope;
  try { scope = workspaces.assert(); } catch (error) { return Promise.reject(error); }
  if (serviceInfo && serviceProcess) return Promise.resolve(serviceInfo);
  if (serviceStartup) return serviceStartup;
  if (quitting) return Promise.reject(new Error('Noteford is closing.'));
  if (serviceProcess) return Promise.reject(new Error('Noteford service is stopping; retry shortly.'));

  ensureRuntimeConfig();
  const serviceLaunch = currentServiceSpec();
  const python = serviceLaunch.command;
  const tokenFile = projectPath('secrets', 'api_token.json');
  const args = [
    ...serviceLaunch.argsPrefix,
    '--config',
    projectPath('config.local.json'),
    '--port',
    '0',
    '--token-file',
    tokenFile,
  ];

  const env = {
    ...process.env,
    ...(!app.isPackaged ? {
      PYTHONPATH: [path.join(projectRoot, 'src'), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    } : {}),
    ...serviceLaunch.extraEnv,
    NOTEFORD_WORKSPACE_ROOT: scope.root,
    NOTEFORD_WORKSPACE_EPOCH: scope.epoch,
    PYTHONUTF8: '1',
    ...(isSmoke ? { NOTEFORD_SMOKE: '1' } : {}),
  };

  const startup = new Promise((resolve, reject) => {
    const child = spawn(python, args, {
      cwd: scope.root,
      env,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    serviceProcess = child;

    let settled = false;
    let stdoutBuffer = '';
    let stderrBuffer = '';

    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        child.kill();
        reject(new Error('Noteford service startup timed out.'));
      }
    }, 15000);

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      stdoutBuffer += chunk;
      const lines = stdoutBuffer.split(/\r?\n/);
      stdoutBuffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const value = JSON.parse(line);
          if (!settled && value.ready === true && Number.isInteger(value.port)) {
            const tokenPayload = JSON.parse(fs.readFileSync(tokenFile, 'utf8'));
            if (!tokenPayload || typeof tokenPayload.token !== 'string') {
              throw new Error('Invalid local API token file.');
            }
            workspaces.assert(scope);
            apiToken = tokenPayload.token;
            serviceInfo = { host: '127.0.0.1', port: value.port };
            settled = true;
            clearTimeout(timeout);
            resolve(serviceInfo);
          }
        } catch (error) {
          if (!settled) {
            settled = true;
            clearTimeout(timeout);
            child.kill();
            reject(error);
          }
        }
      }
    });

    child.stderr.setEncoding('utf8');
    child.stderr.on('data', chunk => {
      stderrBuffer = (stderrBuffer + chunk).slice(-8000);
    });

    child.on('exit', code => {
      if (serviceProcess === child) {
        serviceProcess = null;
        serviceInfo = null;
        apiToken = null;
        serviceRecovery.exited();
      }
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(new Error(`Noteford service exited during startup (code ${code}). 请检查运行环境与数据目录；若已有实例，请先从托盘退出。`));
      }
    });

    child.on('error', error => {
      if (!child.pid && serviceProcess === child) {
        serviceProcess = null;
        serviceInfo = null;
        apiToken = null;
        serviceRecovery.exited();
      }
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(error);
      }
    });
  });
  serviceStartup = startup.finally(() => {
    serviceStartup = null;
  });
  return serviceStartup;
}

async function apiRequest(route, options = {}) {
  const scope = workspaces.assert();
  if (!serviceInfo || !apiToken) await startService();
  workspaces.assert(scope);
  const url = `http://127.0.0.1:${serviceInfo.port}${route}`;
  const request = options.longDownload ? require('./long-download-request.cjs').downloadRequest : fetch;
  const response = await request(url, {
    method: options.method || 'GET',
    headers: {
      'X-Noteford-Token': apiToken,
      ...(macAccountScope?.getStore() ? {'X-Noteford-Library': macAccountScope.getStore()} : {}),
      ...(options.headers || {}),
    },
    body: options.body,
    signal: options.signal ? AbortSignal.any([options.signal, scope.abort.signal]) : scope.abort.signal,
  });
  const payload = await response.json();
  workspaces.assert(scope);
  if (!response.ok || payload.ok === false) {
    const error = new Error(payload.message || payload.error || `HTTP ${response.status}`);
    error.code = payload.error || 'API_ERROR';
    throw error;
  }
  return payload.data ?? payload;
}

function mediaMime(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  const mapping = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.mp4': 'video/mp4',
    '.wav': 'audio/wav',
  };
  return mapping[ext] || 'application/octet-stream';
}

function isAllowedLocalMediaPath(filePath) {
  const resolved = path.resolve(filePath);
  const librariesRoot = path.resolve(projectPath('libraries'));
  if (!resolved.startsWith(librariesRoot + path.sep)) return false;
  try { if (!fs.realpathSync(resolved).startsWith(fs.realpathSync(librariesRoot) + path.sep)) return false; } catch { return false; }
  const relative = path.relative(librariesRoot, resolved);
  const parts = relative.split(path.sep);
  return (
    parts.length >= 4
    && parts[0].startsWith('lib-')
    && parts[1] === 'data'
    && parts[2] === 'media-cache'
  );
}

function registerMediaPath(filePath) {
  if (!filePath) return null;
  const resolved = path.resolve(filePath);
  if (!isAllowedLocalMediaPath(resolved)) return null;
  const id = crypto.createHash('sha256').update(workspaces.assert().epoch + resolved).digest('hex').slice(0, 32);
  mediaMap.set(id, resolved);
  return `noteford-media://asset/${id}`;
}

function allowedRemoteHost(hostname) {
  const host = String(hostname || '').toLowerCase();
  const suffixes = [
    '.qpic.cn',
    '.qlogo.cn',
    '.qq.com',
    '.weixin.qq.com',
    '.wechat.com',
  ];
  return suffixes.some(suffix => host === suffix.slice(1) || host.endsWith(suffix));
}

function registerRemoteUrl(rawUrl, libraryId) {
  if (!rawUrl || !libraryId || !String(libraryId).startsWith('lib-')) return null;
  if (process.platform === 'darwin') rawUrl = require('./mac-remote-media.cjs').normalizeMacMediaUrl(rawUrl);
  try {
    const parsed = new URL(rawUrl);
    if (parsed.protocol !== 'https:' || !allowedRemoteHost(parsed.hostname)) {
      return null;
    }
    const normalized = parsed.toString();
    const id = crypto.createHash('sha256')
      .update(String(libraryId) + '\0' + normalized)
      .digest('hex')
      .slice(0, 32);
    remoteMap.set(id, { url: normalized, libraryId: String(libraryId) });
    return `noteford-remote://asset/${id}`;
  } catch {
    return null;
  }
}

function transformAvatarObject(value) {
  if (process.platform === 'darwin' && value?.avatar_remote_url && !value.avatar_path) {
    return require('./mac-avatar.cjs').remoteAvatar(value, registerRemoteUrl);
  }
  if (!value || !value.avatar_path) return value;
  return {
    ...value,
    avatar_url: registerMediaPath(value.avatar_path),
    avatar_path: undefined,
  };
}

function transformConversationRows(rows) {
  return Array.isArray(rows) ? rows.map(transformAvatarObject) : rows;
}

function transformLibraryRows(rows) {
  return Array.isArray(rows) ? rows.map(transformAvatarObject) : rows;
}

function transformContacts(rows) {
  return Array.isArray(rows) ? rows.map(transformAvatarObject) : rows;
}

function transformSearchPayload(payload) {
  if (!payload || !Array.isArray(payload.items)) return payload;
  payload.items = payload.items.map(item => ({
    ...item,
    chat: transformAvatarObject(item.chat),
    sender: transformAvatarObject(item.sender),
  }));
  return payload;
}


function transformMediaPayload(payload) {
  if (!payload || !Array.isArray(payload.messages)) return payload;
  payload.conversation = transformAvatarObject(payload.conversation);
  const libraryId = payload.library_id || null;
  const allowExpandedMedia = ['following', 'paused'].includes(payload.selection?.state);
  payload.messages = payload.messages.map(message => {
    const next = {
      ...message,
      sender: transformAvatarObject(message.sender),
    };

    if (allowExpandedMedia && message.media?.path) {
      next.media = {
        ...message.media,
        url: registerMediaPath(message.media.path),
        path: undefined,
      };
    }

    if (message.quote?.target) {
      next.quote = {...message.quote, target: transformMediaPayload({
        library_id: libraryId, selection: payload.selection,
        messages: [message.quote.target],
      }).messages[0]};
    }
    if (process.platform === 'darwin' && Array.isArray(message.record_items)) {
      next.record_items = transformMediaPayload({library_id: libraryId, selection: payload.selection,
        messages: message.record_items}).messages;
    }
    if (message.card) {
      next.card = {
        ...message.card,
        cover_url: allowExpandedMedia
          ? registerRemoteUrl(message.card.cover_url, libraryId)
          : null,
        thumbnail_url: allowExpandedMedia
          ? registerRemoteUrl(message.card.thumbnail_url, libraryId)
          : null,
        avatar_url: allowExpandedMedia
          ? registerRemoteUrl(message.card.avatar_url, libraryId)
          : null,
      };
    }

    return next;
  });
  return payload;
}

const { parseRange } = require('./media-range.cjs');

function remoteImageExtension(contentType, bytes) {
  const type = String(contentType || '').split(';')[0].trim().toLowerCase();
  if (type === 'image/jpeg') return 'jpg';
  if (type === 'image/png') return 'png';
  if (type === 'image/gif') return 'gif';
  if (type === 'image/webp') return 'webp';
  if (bytes?.[0] === 0xff && bytes?.[1] === 0xd8) return 'jpg';
  if (bytes?.[0] === 0x89 && bytes?.[1] === 0x50) return 'png';
  if (bytes?.[0] === 0x47 && bytes?.[1] === 0x49) return 'gif';
  if (bytes?.[0] === 0x52 && bytes?.[1] === 0x49) return 'webp';
  return null;
}

function remoteCacheRoot(libraryId) {
  if (!/^lib-[A-Za-z0-9_-]+$/.test(String(libraryId || ''))) return null;
  return projectPath('libraries', String(libraryId), 'data', 'media-cache', 'remote');
}

function findRemoteCache(id, libraryId) {
  const root = remoteCacheRoot(libraryId);
  if (!root || !fs.existsSync(root)) return null;
  const prefix = id + '.';
  const match = fs.readdirSync(root).find(name => name.startsWith(prefix));
  return match ? path.join(root, match) : null;
}

function registerMediaProtocol() {
  protocol.handle('noteford-media', async request => {
    try {
      const scope = workspaces.assert();
      const url = new URL(request.url);
      const id = url.pathname.replace(/^\//, '');
      const filePath = mediaMap.get(id);
      if (!filePath || !fs.existsSync(filePath)) {
        return new Response('Not found', { status: 404 });
      }
      const stat = fs.statSync(filePath);
      if (!stat.isFile()) return new Response('Not found', { status: 404 });
      try {
        const now = new Date();
        fs.utimesSync(filePath, now, now);
      } catch {
        // LRU touch failure must never block media playback.
      }

      const range = parseRange(request.headers.get('range'), stat.size);
      const headers = {
        'Content-Type': mediaMime(filePath),
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-store',
      };

      if (range === false) return new Response(null, {status: 416, headers: {...headers, 'Content-Range': `bytes */${stat.size}`}});
      if (range) {
        const stream = fs.createReadStream(filePath, {
          signal: scope.abort.signal,
          start: range.start,
          end: range.end,
        });
        headers['Content-Range'] = `bytes ${range.start}-${range.end}/${stat.size}`;
        headers['Content-Length'] = String(range.end - range.start + 1);
        return new Response(Readable.toWeb(stream), {
          status: 206,
          headers,
        });
      }

      const stream = fs.createReadStream(filePath, { signal: scope.abort.signal });
      headers['Content-Length'] = String(stat.size);
      return new Response(Readable.toWeb(stream), {
        status: 200,
        headers,
      });
    } catch {
      return new Response('Media error', { status: 500 });
    }
  });

  protocol.handle('noteford-remote', async request => {
    try {
      const scope = workspaces.assert();
      const url = new URL(request.url);
      const id = url.pathname.replace(/^\//, '');
      const remoteEntry = remoteMap.get(id);
      if (!remoteEntry) return new Response('Not found', { status: 404 });
      const remoteUrl = remoteEntry.url;
      const libraryId = remoteEntry.libraryId;

      const cached = findRemoteCache(id, libraryId);
      if (cached && fs.existsSync(cached)) {
        try {
          const now = new Date();
          fs.utimesSync(cached, now, now);
        } catch {}
        const stream = fs.createReadStream(cached, { signal: scope.abort.signal });
        return new Response(Readable.toWeb(stream), {
          status: 200,
          headers: {
            'Content-Type': mediaMime(cached),
            'Cache-Control': 'no-store',
          },
        });
      }

      const response = await net.fetch(remoteUrl, {
        signal: scope.abort.signal,
        headers: {
          'User-Agent': 'Noteford/0.1',
          'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        },
        redirect: 'manual',
      });
      if (response.status >= 300 && response.status < 400) {
        return new Response('Remote redirect blocked', { status: 403 });
      }
      if (!response.ok) {
        return new Response('Remote fetch failed', { status: response.status });
      }

      const finalUrl = new URL(response.url || remoteUrl);
      if (
        finalUrl.protocol !== 'https:'
        || !allowedRemoteHost(finalUrl.hostname)
      ) {
        return new Response('Blocked redirect', { status: 403 });
      }

      const lengthHeader = response.headers.get('content-length');
      if (lengthHeader && Number(lengthHeader) > 8 * 1024 * 1024) {
        return new Response('Remote image too large', { status: 413 });
      }

      const buffer = Buffer.from(await response.arrayBuffer());
      if (buffer.length > 8 * 1024 * 1024) {
        return new Response('Remote image too large', { status: 413 });
      }
      workspaces.assert(scope);
      const contentType = response.headers.get('content-type');
      const ext = remoteImageExtension(contentType, buffer);
      if (!ext) return new Response('Unsupported remote media', { status: 415 });

      const cacheRoot = remoteCacheRoot(libraryId);
      if (!cacheRoot) return new Response('Invalid library', { status: 403 });
      fs.mkdirSync(cacheRoot, { recursive: true });
      const destination = path.join(cacheRoot, `${id}.${ext}`);
      const tmp = destination + '.' + crypto.randomUUID() + '.tmp';
      try {
        fs.writeFileSync(tmp, buffer);
        fs.renameSync(tmp, destination);
      } finally {
        try { fs.unlinkSync(tmp); } catch {}
      }

      return new Response(buffer, {
        status: 200,
        headers: {
          'Content-Type': mediaMime(destination),
          'Content-Length': String(buffer.length),
          'Cache-Control': 'no-store',
        },
      });
    } catch {
      return new Response('Remote image error', { status: 500 });
    }
  });
}

let remoteMcp = null;

function registerIpc(scope) {
  const ipcMain = scopedIpc(scope);
  const runtimeRoot = scope.root;
  const scoped = object => workspaces.guard(scope, object);
  const dialog = new Proxy(require('electron').dialog, { get(target, key) {
    return async (...args) => { workspaces.assert(scope); const result = await target[key](...args); workspaces.assert(scope); return result; };
  }});
  const accountHandlers = new Map();
  const accountChannels = new Set(['status', 'conversations', 'content', 'messages', 'group-members', 'contacts', 'search', 'follow', 'pause', 'refresh', 'avatars', 'media-progress', 'media-recheck', 'media-range', 'save-media-range']);
  const handle = (channel, handler) => {
    if (macAccountScope && accountChannels.has(channel.replace('noteford:', ''))) accountHandlers.set(channel, handler);
    ipcMain.handle(channel, handler);
  };
  if (macAccountScope) {
    ipcMain.handle('noteford:mac-account-request', (event, request = {}) => {
      assertMainRenderer(event);
      const handler = accountHandlers.get(request.channel);
      if (!handler || typeof request.library_id !== 'string') throw new Error('无效的账号请求');
      return macAccountScope.run(request.library_id, () => handler(event, request.input));
    });
    ipcMain.handle('noteford:mac-accounts', event => { assertMainRenderer(event); return apiRequest('/api/v1/mac/accounts'); });
    ipcMain.handle('noteford:mac-accounts-configure', (event, input) => {
      assertMainRenderer(event);
      return apiRequest('/api/v1/mac/accounts', {method: 'POST', body: JSON.stringify(input)});
    });
    ipcMain.handle('noteford:mac-accounts-cancel', event => {
      assertMainRenderer(event);
      return apiRequest('/api/v1/mac/accounts/cancel', {method: 'POST'});
    });
    ipcMain.handle('noteford:mac-accounts-connect', (event, input) => {
      assertMainRenderer(event);
      return apiRequest('/api/v1/mac/accounts/connect', {method: 'POST', body: JSON.stringify(input)});
    });
  }
  handle('noteford:content', (_event, input = {}) => {
    const params = new URLSearchParams();
    for (const key of ['category','query','offset','limit','conversation','link_messages']) if(input[key] !== undefined)params.set(key,String(input[key]));
    return apiRequest('/api/v1/content?' + params.toString());
  });
  const serviceLaunch = currentServiceSpec();
  const serviceEnv = { ...(!app.isPackaged ? { PYTHONPATH: path.join(projectRoot, 'src') } : {}),
    ...serviceLaunch.extraEnv };
  const workerEnv = { ...serviceEnv, NOTEFORD_WORKSPACE_ROOT: scope.root, NOTEFORD_WORKSPACE_EPOCH: scope.epoch };
  if (process.platform === 'darwin') {
    const { createMacAiAccess } = require('./mac-ai-access.cjs');
    const access = scoped(createMacAiAccess({
      runtimeRoot, command: serviceLaunch.command, argsPrefix: serviceLaunch.argsPrefix, env: serviceEnv,
      cliPath: path.join(app.isPackaged ? process.resourcesPath : path.join(desktopRoot, 'mcp-plugin'), 'cli', 'noteford-ai'),
    }));
    ipcMain.handle('noteford:mac-ai-status', event => { assertMainRenderer(event); return access.status(); });
    ipcMain.handle('noteford:mac-ai-configure', async (event, input = {}) => {
      assertMainRenderer(event);
      const current = (await apiRequest('/api/v1/libraries')).find(item => item.is_current);
      return access.configure(input, current);
    });
    ipcMain.handle('noteford:mac-ai-copy', (event, input = {}) => {
      assertMainRenderer(event);
      if (!['mcp', 'cli'].includes(input.format)) throw new Error('无效的连接格式');
      clipboard.writeText(input.format === 'mcp' ? JSON.stringify(access.config(), null, 2) : access.cliInstructions());
    });
  }
  const connectionState = scoped(require('./mcp-connection-state.cjs').createMcpConnectionState({ runtimeRoot }));
  ipcMain.handle('noteford:mcp-connection-status', event => { assertMainRenderer(event); return connectionState.status(); });
  ipcMain.handle('noteford:mcp-connection-configure', (event, input = {}) => { assertMainRenderer(event); return connectionState.configure(input); });
  const { createMcpConnections } = require('./mcp-connections.cjs');
  const mcpConnections = scoped(createMcpConnections({
    runtimeRoot,
    command: serviceLaunch.command,
    argsPrefix: serviceLaunch.argsPrefix,
    env: serviceEnv,
    pluginTemplate: path.join(desktopRoot, 'mcp-plugin'),
  }));
  ipcMain.handle('noteford:mcp-list', () => mcpConnections.list());
  const { createRemoteMcp } = require('./remote-mcp.cjs');
  remoteMcp = createRemoteMcp({
    runtimeRoot, assertActive: () => workspaces.assert(scope),
    command: serviceLaunch.command,
    argsPrefix: serviceLaunch.argsPrefix,
    env: workerEnv,
  });
  try { remoteMcp.start(); } catch (error) { console.error('remote MCP not started', error.message); }
  ipcMain.handle('noteford:remote-mcp-status', () => remoteMcp.status());
  ipcMain.handle('noteford:remote-mcp-configure', (_event, input = {}) => remoteMcp.configure(input));
  ipcMain.handle('noteford:remote-mcp-pair-code', () => remoteMcp.pairCode());
  ipcMain.handle('noteford:remote-mcp-pending', () => remoteMcp.pending());
  ipcMain.handle('noteford:remote-mcp-approve', (_event, input = {}) => {
    const grant = mcpConnections.list().find(item => item.id === input.grant_client && item.enabled);
    if (!grant) throw new Error('所选连接不存在或已撤销');
    return remoteMcp.approve(String(input.display_id), grant.id);
  });
  ipcMain.handle('noteford:remote-mcp-deny', (_event, input = {}) => remoteMcp.deny(String(input.display_id)));
  ipcMain.handle('noteford:remote-mcp-grants', () => remoteMcp.grants());
  ipcMain.handle('noteford:remote-mcp-revoke', (_event, input = {}) => remoteMcp.revoke(String(input.id)));
  ipcMain.handle('noteford:mcp-create', async (_event, input = {}) => {
    const libraries = await apiRequest('/api/v1/libraries');
    const current = libraries.find(item => item.is_current);
    if (!current || current.id !== input.library_id) throw new Error('账号已切换，请重新选择会话');
    if (['all_synced', 'all_indexed'].includes(input.scope)) return mcpConnections.create(input);
    const known = new Set();
    for (let offset = 0; ; offset += 500) {
      const rows = await apiRequest('/api/v1/conversations?limit=500&offset=' + offset);
      for (const row of rows) if (row.has_local_messages) known.add(row.username);
      if (rows.length < 500) break;
    }
    const latest = (await apiRequest('/api/v1/libraries')).find(item => item.is_current);
    if (latest?.id !== input.library_id) throw new Error('账号已切换，请重新选择会话');
    if (!Array.isArray(input.conversations) || input.conversations.some(id => !known.has(id))) throw new Error('只能授权当前账号中已建立索引的会话');
    return mcpConnections.create(input);
  });
  ipcMain.handle('noteford:mcp-revoke', (_event, input = {}) => { mcpConnections.revoke(input.id); });
  const { createLocalPairing } = require('./local-pairing.cjs');
  const localPairing = scoped(createLocalPairing({
    runtimeRoot,
    command: serviceLaunch.command,
    argsPrefix: serviceLaunch.argsPrefix,
    env: workerEnv,
  }));
  ipcMain.handle('noteford:mcp-copy', async (_event, input = {}) => {
    mcpConnections.config(input.id, 'nfp_check');
    clipboard.writeText(JSON.stringify(mcpConnections.config(input.id, await localPairing.issue(input.id)), null, 2));
  });
  const { buildConnectPrompt } = require('./mcp-prompt.cjs');
  ipcMain.handle('noteford:mcp-copy-prompt', async () => {
    const skillPath = app.isPackaged
      ? path.join(process.resourcesPath, 'skills', 'noteford')
      : path.join(desktopRoot, 'mcp-plugin', 'skills', 'noteford');
    clipboard.writeText(buildConnectPrompt({ skillPath, platform: process.platform }));
    return { copied: true };
  });
  ipcMain.handle('noteford:skill-install-command', async (_event, input = {}) => {
    const command = 'npx skills add Followhongyun/Noteford-Community --skill noteford-community';
    if (input.copy === true) clipboard.writeText(command);
    return { command, copied: input.copy === true };
  });
  const { createLocalConsent } = require('./local-consent.cjs');
  const requestConsent = createLocalConsent({
    pairing: localPairing, connections: mcpConnections,
    listLibraries: () => apiRequest('/api/v1/libraries'),
    confirm: async (request) => {
      const answer = await dialog.showMessageBox(mainWindow, {
        type: 'question', buttons: ['允许', '拒绝'], defaultId: 1, cancelId: 1, noLink: true,
        title: 'Noteford · AI 连接请求',
        message: `「${request.client_name}」请求连接 Noteford`,
      });
      workspaces.assert(scope);
      return answer.response === 0;
    },
    onError: () => console.warn('Noteford local connection approval did not complete'),
  });
  const pairingStore = path.join(runtimeRoot, 'data', 'mcp_local.store');
  let pairingRefreshTimer = null;
  const refreshPairingPending = () => {
    clearTimeout(pairingRefreshTimer);
    pairingRefreshTimer = setTimeout(() => {
      try { void workspaces.run(scope, requestConsent).catch(() => undefined); } catch {}
    }, 180);
  };
  fs.watchFile(pairingStore, { interval: 1000 }, refreshPairingPending);
  refreshPairingPending();
  localPairingWatch = () => {
    clearTimeout(pairingRefreshTimer);
    fs.unwatchFile(pairingStore, refreshPairingPending);
  };
  ipcMain.handle('noteford:mcp-pair-bindings', event => { assertMainRenderer(event); return localPairing.bindings(); });
  ipcMain.handle('noteford:mcp-pair-revoke', (event, input = {}) => { assertMainRenderer(event); return localPairing.revoke(String(input.id)); });
  ipcMain.handle('noteford:mcp-export', async (_event, input = {}) => {
    const result = await dialog.showOpenDialog(mainWindow, { title: '选择官方插件导出位置', properties: ['openDirectory', 'createDirectory'] });
    if (result.canceled || !result.filePaths[0]) return null;
    mcpConnections.config(input.id, 'nfp_check');
    const destination = mcpConnections.exportPlugin(input.id, result.filePaths[0], await localPairing.issue(input.id));
    shell.showItemInFolder(destination);
    return destination;
  });
  handle('noteford:status', () => apiRequest('/api/v1/status'));
  handle('noteford:media-range', (_event, input = {}) => {
    const query = new URLSearchParams({library_id: input.library_id, conversation: input.conversation});
    return apiRequest(`/api/v1/conversation-media-range?${query}`);
  });
  handle('noteford:save-media-range', (_event, input = {}) => apiRequest('/api/v1/conversation-media-range', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(input),
  }));
  handle('noteford:avatars', async (_event, input = {}) => {
    const query = new URLSearchParams({library_id: input.library_id, usernames: JSON.stringify(input.usernames)});
    return transformConversationRows(await apiRequest(`/api/v1/avatars?${query}`));
  });
  ipcMain.handle('noteford:settings', () => apiRequest('/api/v1/settings'));
  ipcMain.handle('noteford:auto-media-limit', (_event, max_mb) => apiRequest('/api/v1/settings/auto-media-limit', {method: 'POST', body: JSON.stringify({max_mb})}));
  ipcMain.handle('noteford:auto-media-loading', (_event, enabled) => apiRequest('/api/v1/settings/auto-media-loading', {method: 'POST', body: JSON.stringify({enabled})}));
  ipcMain.handle('noteford:parse-stickers', (_event, enabled) => apiRequest('/api/v1/settings/parse-stickers', {method: 'POST', body: JSON.stringify({enabled})}));
  ipcMain.handle('noteford:sync-interval', (_event, minutes) => apiRequest('/api/v1/settings/sync-interval', {method: 'POST', body: JSON.stringify({minutes})}));
  ipcMain.handle('noteford:sync-interval-seconds', (_event, seconds) => apiRequest('/api/v1/settings/sync-interval', {method: 'POST', body: JSON.stringify({seconds})}));
  ipcMain.handle('noteford:save-media-window', async (_event, input = {}) => {
    if (!Number.isInteger(input.days) || input.days < 1 || input.days > 365) {
      throw new Error('媒体解析天数须为 1 到 365 天');
    }
    return apiRequest('/api/v1/media-window', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days: input.days }),
    });
  });
  handle('noteford:conversations', async (_event, input = {}) => {
    const params = new URLSearchParams();
    params.set('limit', String(input.limit || 120));
    if (input.offset) params.set('offset', String(input.offset));
    if (input.query) params.set('q', String(input.query));
    if (input.state) params.set('state', String(input.state));
    return transformConversationRows(
      await apiRequest('/api/v1/conversations?' + params.toString())
    );
  });
  handle('noteford:media-progress', async (_event, input = {}) => apiRequest('/api/v1/media/progress?' + new URLSearchParams({library_id: input.library_id, request_id: input.request_id})));
  handle('noteford:media-recheck', async (_event, input = {}) => {
    const payload = await apiRequest('/api/v1/media/recheck', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({library_id: input.library_id, uid: input.uid, request_id: input.request_id}),
      longDownload: true,
    });
    return transformMediaPayload(payload).messages[0].media;
  });
  handle('noteford:messages', async (_event, input = {}) => {
    const params = new URLSearchParams();
    params.set('conversation', String(input.conversation || ''));
    params.set('limit', String(input.limit || 80));
    if (input.before) params.set('before', String(input.before));
    if (input.before_uid) params.set('before_uid', String(input.before_uid));
    if (input.around_uid) params.set('around_uid', String(input.around_uid));
    if (input.at_time != null) params.set('at_time', String(input.at_time));
    if (input.after_uid) params.set('after_uid', String(input.after_uid));
    if (input.category) params.set('category', String(input.category));
    if (input.query) params.set('q', String(input.query));
    if (input.from_day) params.set('from_day', String(input.from_day));
    if (input.to_day) params.set('to_day', String(input.to_day));
    if (input.sender) params.set('sender', String(input.sender));
    if (input.filter_offset) params.set('filter_offset', String(input.filter_offset));
    const payload = await apiRequest('/api/v1/messages?' + params.toString());
    return transformMediaPayload(payload);
  });
  handle('noteford:group-members', async (_event, input = {}) => {
    const params = new URLSearchParams();
    params.set('conversation', String(input.conversation || ''));
    const payload = await apiRequest('/api/v1/conversations/members?' + params.toString());
    return {
      ...payload,
      members: Array.isArray(payload.members)
        ? payload.members.map(transformAvatarObject)
        : [],
    };
  });
  handle('noteford:contacts', async (_event, input = {}) => {
    const params = new URLSearchParams();
    params.set('limit', String(input.limit || 300));
    if (input.offset) params.set('offset', String(input.offset));
    if (input.query) params.set('q', String(input.query));
    return transformContacts(
      await apiRequest('/api/v1/contacts?' + params.toString())
    );
  });
  handle('noteford:search', async (_event, input = {}) => {
    const params = new URLSearchParams();
    params.set('q', String(input.query || ''));
    params.set('limit', String(input.limit || 80));
    return transformSearchPayload(
      await apiRequest('/api/v1/search?' + params.toString())
    );
  });
  ipcMain.handle('noteford:open-external', async (_event, input = {}) => {
    const raw = String(input.url || '');
    const parsed = new URL(raw);
    if (!['https:', 'http:'].includes(parsed.protocol)) {
      throw new Error('Blocked external URL');
    }
    await shell.openExternal(parsed.toString());
    return true;
  });
  if (process.platform === 'darwin') ipcMain.handle('noteford:mac-save-media', async (event, input = {}) => {
    assertMainRenderer(event);
    return require('./media-save.cjs').saveMedia(input, {
      mediaMap, isAllowedLocalMediaPath,
      showSaveDialog: options => dialog.showSaveDialog(mainWindow, options),
    });
  });
  if (process.platform === 'darwin') ipcMain.handle('noteford:mac-reveal-media', async (_event, input = {}) => {
    const parsed = new URL(String(input.url || ''));
    const id = parsed.pathname.slice(1);
    const candidate = mediaMap.get(id);
    if (parsed.protocol !== 'noteford-media:' || parsed.hostname !== 'asset' || !candidate
      || !isAllowedLocalMediaPath(fs.realpathSync(candidate))) throw new Error('Blocked media path');
    shell.showItemInFolder(candidate);
    return true;
  });
  ipcMain.handle('noteford:libraries', async () => {
    return transformLibraryRows(await apiRequest('/api/v1/libraries'));
  });
  ipcMain.handle('noteford:library-candidates', async (_event, input = {}) => {
    return apiRequest('/api/v1/libraries/candidates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        wechat_root: input.wechat_root ? String(input.wechat_root) : null,
      }),
    });
  });
  ipcMain.handle('noteford:library-register', async (_event, input = {}) => {
    const result = await apiRequest('/api/v1/libraries/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ library_id: String(input.library_id || '') }),
    });
    return transformAvatarObject(result);
  });
  ipcMain.handle('noteford:library-switch', async (_event, input = {}) => {
    const result = await apiRequest('/api/v1/libraries/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        library_id: String(input.library_id || ''),
        initialize: input.initialize !== false,
      }),
    });
    if (result?.library) {
      result.library = transformAvatarObject(result.library);
    }
    return result;
  });
  ipcMain.handle('noteford:library-rename', async (_event, input = {}) => {
    const result = await apiRequest('/api/v1/libraries/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        library_id: String(input.library_id || ''),
        display_name: String(input.display_name || ''),
      }),
    });
    return transformAvatarObject(result);
  });
  ipcMain.handle('noteford:library-discover', async (_event, input = {}) => {
    const rows = await apiRequest('/api/v1/libraries/discover', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        wechat_root: input.wechat_root ? String(input.wechat_root) : null,
      }),
    });
    return transformLibraryRows(rows);
  });
  const discoveryDir = () => seam.discoveryDirectory({
    platform: process.platform,
    localAppData: process.env.LOCALAPPDATA,
    home: os.homedir(),
    appData: app.getPath('appData'),
  });
  const readFullDisk = () => seam.collectFullDiskReads(os.homedir(), fs);
  const permissionRuntime = process.platform === 'darwin' ? require('./mac-permission-runtime.cjs') : null;
  const readDiskPolicy = permissionRuntime?.createFullDiskPolicyReader();
  const signatureNotice = permissionRuntime ? permissionRuntime.readSignatureNotice({
    bundle: require('./mac-permissions.cjs').appBundlePath(process.execPath),
    marker: path.join(app.getPath('userData'), 'permission-signature.json'),
  }) : Promise.resolve('');
  let diskSettingsOpened = false;
  const readPermissionStatuses = async () => {
    if (process.platform !== 'darwin') return {};
    const mac = require('./mac-permissions.cjs');
    const [automation, policyAllowed] = await Promise.all([
      mac.readAutomationStatus({ timeoutMs: 500 }), readDiskPolicy(),
    ]);
    return seam.statusesFromProbes({
      automation: automation.ok ? { ok: true, results: automation.results } : { ok: false, results: [] },
      reads: readFullDisk(),
      policyAllowed: policyAllowed === true,
    });
  };
  const permissionMonitor = permissionRuntime?.createPermissionMonitor({
    read: async () => {
      const guide = seam.permissionGuide({ platform: process.platform, statuses: await readPermissionStatuses() });
      const notice = await signatureNotice;
      for (const card of guide.cards) {
        if (card.status !== 'granted') card.identityNote = notice;
        if (card.id === 'full_disk') card.canRestart = card.status === 'restart_required' || (diskSettingsOpened && card.status !== 'granted');
      }
      return guide;
    },
    publish: guide => {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('noteford:permission-state', guide);
    },
  });
  if (permissionMonitor) {
    const unbindFocus = permissionRuntime.bindPermissionFocus(app, permissionMonitor.refresh);
    workspaceCleanup.push(unbindFocus);
  }
  const openMacSettings = url => {
    const opened = spawn('/usr/bin/open', [String(url)], { detached: true, stdio: 'ignore' });
    opened.on('error', () => {});
    opened.unref();
    const settings = spawn('/usr/bin/open', ['-a', 'System Settings'], { detached: true, stdio: 'ignore' });
    settings.on('error', () => {});
    settings.unref();
  };
  const lowerForPermissionPrompt = () => {
    const window = mainWindow;
    if (!window || window.isDestroyed()) return;
    window.setAlwaysOnTop(false);
    window.blur();
    window.hide();
  };
  const revealPermissionWindow = focus => {
    const window = mainWindow;
    if (!window || window.isDestroyed()) return;
    if (focus) {
      window.show();
      window.focus();
      return;
    }
    window.showInactive();
  };
  const writePermissionMarker = record => {
    const directory = discoveryDir();
    const marker = seam.permissionMarkerPath(directory);
    fs.mkdirSync(directory, { recursive: true });
    const payload = { ...record, at: new Date().toISOString() };
    const temporary = marker + '.' + crypto.randomUUID() + '.tmp';
    fs.writeFileSync(temporary, JSON.stringify(payload, null, 2));
    fs.renameSync(temporary, marker);
  };
  ipcMain.handle('noteford:host-profile', () => {
    return seam.hostProfile({ platform: process.platform, permissionsPending: false });
  });
  ipcMain.handle('noteford:permission-guide', async () => {
    if (permissionMonitor) return permissionMonitor.refresh();
    return seam.permissionGuide({ platform: process.platform, statuses: await readPermissionStatuses() });
  });
  ipcMain.handle('noteford:permission-restart', event => {
    assertMainRenderer(event);
    return permissionRuntime ? permissionRuntime.restartForPermissions(app, process.platform, isLocalLive ? { requestRestart: () => {
      if (!process.connected) return false;
      process.send({ type: 'noteford:request-restart' });
      return true;
    } } : {}) : { ok: false };
  });
  ipcMain.handle('noteford:permission-grant', async (_event, input = {}) => {
    const id = String(input.id || '');
    if (process.platform !== 'darwin') {
      return seam.executePermissionGrant({
        platform: process.platform,
        id,
        current: 'unknown',
        beforeResults: [],
        prompt: () => ({ results: [], elapsedMs: 0, incomplete: false }),
        openSettings: () => {},
      });
    }
    const mac = require('./mac-permissions.cjs');
    if (id === 'full_disk') {
      diskSettingsOpened = true;
      return mac.presentFullDiskGrant({
        current: seam.fullDiskStatus(readFullDisk()),
        execPath: process.execPath,
        openSettings: openMacSettings,
        revealApp: bundle => shell.showItemInFolder(bundle),
      });
    }
    if (id !== 'automation') {
      const result = seam.executePermissionGrant({
        platform: process.platform,
        id,
        current: seam.fullDiskStatus(readFullDisk()),
        beforeResults: [],
        prompt: () => ({ results: [], elapsedMs: 0, incomplete: false }),
        openSettings: openMacSettings,
      });
      return result;
    }
    const before = await mac.readAutomationStatus({ timeoutMs: 300 });
    const wechatRunning = !before.results.some(row => row.target === 'wechat' && [-600, -10814].includes(row.code));
    return mac.presentAutomationGrant({
      current: 'unknown',
      wechatRunning,
      visibleMs: seam.GRANT_VISIBLE_MS,
      readStatus: async () => {
        const again = await mac.readAutomationStatus({ timeoutMs: 400 });
        return { status: again.ok ? seam.automationCardStatus(again.results) : 'unknown' };
      },
      startPrompt: () => {
        const prompt = mac.requestAutomationConsent();
        prompt.done.finally(() => { void permissionMonitor.refresh({ force: true }).catch(() => {}); });
        return prompt;
      },
      openSettings: openMacSettings,
      lowerWindow: lowerForPermissionPrompt,
      revealWindow: revealPermissionWindow,
    });
  });
  ipcMain.handle('noteford:auto-capture-status', async () => {
    if (process.platform !== 'darwin') {
      return { ok: true, needs_scan: false, scan_status: 'done', keys_already_valid: true, error: 'not_required' };
    }
    return apiRequest('/api/v1/mac/auto-capture');
  });
  ipcMain.handle('noteford:auto-capture-start', async (_event, input = {}) => {
    if (process.platform !== 'darwin') return { ok: false, error: 'not_required' };
    return apiRequest('/api/v1/mac/auto-capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: input.force !== false }),
    });
  });
  ipcMain.handle('noteford:permission-complete', async () => {
    const statuses = await readPermissionStatuses();
    const guide = seam.permissionGuide({ platform: process.platform, statuses });
    return seam.completePermissionGuide({
      platform: process.platform,
      canContinue: guide.canContinue,
      statuses,
      writeMarker: writePermissionMarker,
    });
  });
  ipcMain.handle('noteford:pick-library-root', async () => {
    const result = await dialog.showOpenDialog({
      title: '选择微信 xwechat_files 或备份目录',
      properties: ['openDirectory'],
    });
    return result.canceled ? null : (result.filePaths[0] || null);
  });
  handle('noteford:follow', async (_event, input = {}) => {
    return apiRequest('/api/v1/conversations/follow', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: String(input.username || '') }),
    });
  });
  handle('noteford:pause', async (_event, input = {}) => {
    return apiRequest('/api/v1/conversations/pause', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: String(input.username || '') }),
    });
  });
  handle('noteford:refresh', () => apiRequest('/api/v1/refresh', { method: 'POST' }));
  if (process.platform === 'darwin') {
    require('./mac-wechat.cjs').registerMacWechat(ipcMain, {
      python: process.env.NOTEFORD_PYTHON || 'python3',
      src: path.join(projectRoot, 'src'),
      runtimeRoot, scope, assertActive: () => workspaces.assert(scope),
    });
  }
}

function publishWorkspaceDiscovery() {
  if ((app.isPackaged || isLocalLive) && seam.shouldPublishDiscovery({ smoke: isSmoke })) {
    unpublishMcpDiscovery?.();
    const published = currentServiceSpec();
    unpublishMcpDiscovery = require('./mcp-discovery.cjs').publishDiscovery({
      directory: seam.discoveryDirectory({
        platform: process.platform,
        localAppData: process.env.LOCALAPPDATA,
        home: os.homedir(),
        appData: app.getPath('appData'),
      }),
      command: published.command,
      args: published.argsPrefix,
      config: path.join(runtimeRoot, 'config.local.json'),
      appExecutable: process.execPath, pid: process.pid,
      skillPath: app.isPackaged
        ? path.join(process.resourcesPath, 'skills', 'noteford')
        : path.join(desktopRoot, 'mcp-plugin', 'skills', 'noteford'),
    });
  }
}

async function createWindow() {
  Menu.setApplicationMenu(null);
  registerMediaProtocol();
  if (process.platform === 'darwin') {
    const corners = require('./mac-corners.cjs').createNativeCorners();
    ipcMain.handle('noteford:native-corners', (event, input) => {
      assertMainRenderer(event);
      return corners.render(input);
    });
    app.once('will-quit', () => corners.close());
  }
  await activateWorkspace();
  ipcMain.handle('noteford:workspace-ready',event=>{assertMainRenderer(event);return {epoch:workspaces.assert().epoch};});
  const window = new BrowserWindow({
    width: 1240,
    height: 820,
    icon: appIcon,
    minWidth: 940,
    minHeight: 640,
    show: false,
    backgroundColor: '#f5f5f5',
    title: isDev ? `${productName} Dev` : productName,
    autoHideMenuBar: true,
    ...windowChrome(process.platform),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow = window;

  if (process.platform === 'win32') {
    window.setAppDetails({
      appId: appUserModelId,
      appIconPath: appIcon,
      appIconIndex: 0,
      relaunchCommand: isLocalLive
        ? `powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "${path.join(projectRoot, 'Scripts', 'start-live.ps1')}" -DataRoot "${runtimeRoot}"`
        : app.isPackaged
        ? `"${process.execPath}"`
        : `"${process.execPath}" "${desktopRoot}"${isDev ? ' --dev' : ''}`,
      relaunchDisplayName: isDev ? `${productName} Dev` : productName,
    });
  }

  if (isDev) {
    window.webContents.on('page-title-updated', event => {
      event.preventDefault();
      window.setTitle(`${productName} Dev`);
    });
  }

  if (!isSmoke && !process.argv.includes('--background')) window.once('ready-to-show', () => window.show());

  if (useLiveRenderer) {
    await window.loadURL('http://127.0.0.1:5187');
  } else {
    await window.loadFile(path.join(desktopRoot, 'dist', 'index.html'));
  }



  if (isLocalLive && process.send) {
    const mounted = await window.webContents.executeJavaScript(`new Promise(resolve => {
      const deadline = Date.now() + 5000;
      const check = () => document.querySelector('.app-shell, .product-welcome') ? resolve(true) : Date.now() >= deadline ? resolve(false) : setTimeout(check, 100);
      check();
    })`);
    if (!mounted) throw new Error('Local renderer did not mount');
    process.send({ type: 'noteford:ready', runtimeRoot: installationRoot, servicePort: serviceInfo?.port, servicePid: serviceProcess?.pid });
  }

  if (isSmoke) {
    await new Promise(resolve => setTimeout(resolve, 500));
    if (process.env.NOTEFORD_SMOKE_SCREENSHOT) {
      await window.webContents.executeJavaScript(`
        new Promise(resolve => {
          let attempts = 0;
          const poll = () => {
            if (document.querySelector('.product-welcome') || document.querySelector('.library-onboarding')?.getAttribute('data-scan-complete') === 'true' || attempts++ >= 80) resolve(true);
            else setTimeout(poll, 250);
          };
          poll();
        })
      `);
      window.setPosition(-10000, -10000);
      window.showInactive();
      await new Promise(resolve => setTimeout(resolve, 800));
      const screenshot = await window.webContents.capturePage();
      fs.writeFileSync(process.env.NOTEFORD_SMOKE_SCREENSHOT, screenshot.toPNG());
      window.hide();
    }
    const result = await window.webContents.executeJavaScript(`
      (async () => {
        const status = await window.noteford.status();
        const libraries = await window.noteford.libraries();
        const conversations = await window.noteford.conversations({ limit: 200 });
        const first = conversations[0] || null;
        const page = first
          ? await window.noteford.messages({ conversation: first.username, limit: 5 })
          : { messages: [] };
        if (!libraries.length) {
          for (let attempt = 0; attempt < 80; attempt += 1) {
            if (document.querySelector('.product-welcome') || document.querySelector('.library-onboarding')?.getAttribute('data-scan-complete') === 'true') break;
            await new Promise(resolve => setTimeout(resolve, 250));
          }
        }
        const candidateCardCount = document.querySelectorAll('.library-candidate').length;
        const candidateAvatarCount = document.querySelectorAll('.library-candidate img.avatar-image').length;

        let avatarProbe = null;
        const avatarConversation = conversations.find(item => item.avatar_url);
        if (avatarConversation?.avatar_url) {
          const response = await fetch(avatarConversation.avatar_url);
          const bytes = new Uint8Array(await response.arrayBuffer());
          avatarProbe = {
            httpStatus: response.status,
            contentType: response.headers.get('content-type'),
            byteCount: bytes.length,
          };
        }

        let cardProbe = null;
        const linkConversation = conversations.find(item => item.latest_kind === 'link');
        if (linkConversation) {
          const linkPage = await window.noteford.messages({
            conversation: linkConversation.username,
            limit: 20,
          });
          const cardMessage = linkPage.messages.find(item =>
            item.card?.cover_url || item.card?.thumbnail_url
          );
          const cardUrl = cardMessage?.card?.cover_url || cardMessage?.card?.thumbnail_url;
          if (cardUrl) {
            const response = await fetch(cardUrl);
            const bytes = new Uint8Array(await response.arrayBuffer());
            cardProbe = {
              httpStatus: response.status,
              contentType: response.headers.get('content-type'),
              byteCount: bytes.length,
            };
          }
        }

        let emojiProbe = null;
        const followed = await window.noteford.conversations({
          limit: 50,
          state: 'following',
        });
        for (const row of followed) {
          if (emojiProbe) break;
          const emojiPage = await window.noteford.messages({
            conversation: row.username,
            limit: 300,
          });
          const emojiMessage = emojiPage.messages.find(item =>
            (item.type.kind === 'emoji' || item.type.kind === 'gif')
            && (item.card?.thumbnail_url || item.card?.cover_url)
          );
          const emojiUrl = emojiMessage?.card?.thumbnail_url || emojiMessage?.card?.cover_url;
          if (emojiUrl) {
            const response = await fetch(emojiUrl);
            const bytes = new Uint8Array(await response.arrayBuffer());
            emojiProbe = {
              httpStatus: response.status,
              contentType: response.headers.get('content-type'),
              byteCount: bytes.length,
            };
          }
        }

        let gifProbe = null;
        for (const row of followed) {
          if (gifProbe) break;
          const gifPage = await window.noteford.messages({
            conversation: row.username,
            limit: 300,
          });
          const gifMessage = gifPage.messages.find(item =>
            item.type.kind === 'gif'
            && (item.card?.thumbnail_url || item.card?.cover_url)
          );
          const gifUrl = gifMessage?.card?.thumbnail_url || gifMessage?.card?.cover_url;
          if (gifUrl) {
            const response = await fetch(gifUrl);
            const bytes = new Uint8Array(await response.arrayBuffer());
            gifProbe = {
              httpStatus: response.status,
              contentType: response.headers.get('content-type'),
              byteCount: bytes.length,
            };
          }
        }

        let mediaProbe = null;
        const mediaConversation = conversations.find(item =>
          item.latest_kind === 'image' || item.latest_kind === 'video'
        );
        if (mediaConversation) {
          const mediaPage = await window.noteford.messages({
            conversation: mediaConversation.username,
            limit: 40,
          });
          const mediaMessage = mediaPage.messages.find(item => item.media?.url);
          if (mediaMessage?.media?.url) {
            const response = await fetch(mediaMessage.media.url, {
              headers: { Range: 'bytes=0-31' },
            });
            const bytes = new Uint8Array(await response.arrayBuffer());
            mediaProbe = {
              kind: mediaMessage.type.kind,
              status: mediaMessage.media.status || null,
              httpStatus: response.status,
              contentType: response.headers.get('content-type'),
              byteCount: bytes.length,
            };
          }
        }

        const railNavTitles = Array.from(
          document.querySelectorAll('.rail-nav button')
        ).map(button => button.getAttribute('title'));
        const railBottomTitles = Array.from(
          document.querySelectorAll('.rail-bottom button')
        ).map(button => button.getAttribute('title'));
        const firstRunActionPresent = Array.from(document.querySelectorAll('button'))
          .some(button => button.textContent?.includes('添加本地微信资料库'));
        const cloudTogglePresent = !!document.querySelector('.cloud-toggle input[type="checkbox"]');
        const searchBoxCount = document.querySelectorAll('.search-box').length;
        const chatHeaderPresent = !!document.querySelector('.chat-header');
        const readOnlyFooterPresent = !!document.querySelector('.read-only-footer');

        const conversationScroll = document.querySelector('.conversation-scroll');
        const scrollMetrics = conversationScroll ? {
          clientHeight: conversationScroll.clientHeight,
          scrollHeight: conversationScroll.scrollHeight,
          overflowY: getComputedStyle(conversationScroll).overflowY,
          scrollable: conversationScroll.scrollHeight > conversationScroll.clientHeight,
        } : null;

        const directoryButton = Array.from(
          document.querySelectorAll('.rail-nav button')
        ).find(button => button.getAttribute('title') === '通讯录');
        directoryButton?.click();
        await new Promise(resolve => setTimeout(resolve, 450));
        const directoryRows = document.querySelectorAll('.directory-row').length;
        const directoryScroll = document.querySelector('.conversation-scroll');
        const directoryMetrics = directoryScroll ? {
          clientHeight: directoryScroll.clientHeight,
          scrollHeight: directoryScroll.scrollHeight,
          scrollable: directoryScroll.scrollHeight > directoryScroll.clientHeight,
        } : null;

        const digestPagePresent = false;
        const updater = await window.noteford.updateStatus();

        return {
          domShell: !!document.querySelector('.app-shell'),
          nativeMenuHidden: !document.body.innerText.includes('File Edit View Window'),
          messagesIndexed: status.messages,
          libraryRows: libraries.length,
          candidateCardCount,
          candidateAvatarCount,
          currentLibraryId: status.library_id,
          librarySwitcherMounted: !!document.querySelector('.library-avatar-button'),
          railNavTitles,
          railBottomTitles,
          firstRunActionPresent,
          cloudTogglePresent,
          searchBoxCount,
          chatHeaderPresent,
          readOnlyFooterPresent,
          conversationRows: conversations.length,
          directoryRows,
          directoryMetrics,
          digestPagePresent,
          updaterVersion: updater.version,
          messageRows: page.messages.length,
          avatarProbe,
          cardProbe,
          emojiProbe,
          gifProbe,
          mediaProbe,
          scrollMetrics,
        };
      })()
    `, true);
    console.log(JSON.stringify({ smoke: true, ...result }));
    await new Promise(resolve => setTimeout(resolve, 250));
    app.quit();
  }
  if (!isSmoke) {
    powerMonitor.on('resume', () => serviceRecovery.resume());
    const showFromTray = () => {
      if (window.isMinimized()) window.restore();
      window.show();
      window.focus();
    };
    try {
      const image = seam.presentTray(process.platform, nativeImage, seam.trayIconPath(iconPaths));
      tray = new Tray(image.isEmpty() ? appIcon : image);
      tray.setToolTip(isDev ? `${productName} Dev` : productName);
      const menu = seam.trayMenuTemplate(process.platform).map(item => {
        if (item.action === 'show') return { label: item.label, click: showFromTray };
        if (item.action === 'quit') return { label: item.label, click: () => app.quit() };
        return { type: 'separator' };
      });
      tray.setContextMenu(Menu.buildFromTemplate(menu));
      tray.on('click', showFromTray);
      tray.on('double-click', showFromTray);
    } catch (error) {
      tray = null;
      console.error('Noteford tray unavailable', error && error.message ? error.message : error);
    }
    window.on('close', event => {
      if (!quitting && seam.hideOnClose(process.platform, tray)) {
        event.preventDefault();
        window.hide();
      }
    });
    seam.bindReopen(process.platform, app, () => mainWindow);
  }
}

const ownsInstance = isSmoke || app.requestSingleInstanceLock();
if (!ownsInstance) app.quit();
else {
  workspaces = new LocalWorkspace(installationRoot);
  app.whenReady().then(createWindow).catch(error => {
  console.error(error);
  if (!isSmoke) {
    dialog.showErrorBox(`${productName} 启动失败`, '本地读取服务未能启动。请检查应用数据目录的访问权限，然后重新启动应用。');
  }
  app.quit();
  });
}

app.on('second-instance', (_event, commandLine) => {
  try { unpublishMcpDiscovery?.refresh(); } catch { /* periodic discovery repair will retry */ }
  if(commandLine.includes('--background'))return;
  if (mainWindow) { mainWindow.show(); if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.focus(); }
});
// The local supervisor requests a normal exit before launching changed code.
if (isLocalLive) process.on('message', message => {
  if (message?.type === 'noteford:reload') app.quit();
});

app.on('before-quit', () => {
  quitting = true;
  workspaces?.lock();
  serviceRecovery.stop();
  unpublishMcpDiscovery?.();
  localPairingWatch?.();
  tray?.destroy();
  remoteMcp?.stop();
  if (serviceProcess) {
    serviceProcess.kill();
    serviceProcess = null;
  }
});

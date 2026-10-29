'use strict';

const path = require('node:path');
const guide = require('./permission-guide.cjs');

const WINDOWS_INTRO = '已从微信记录的数据位置查找账号。选择账号后才建立本地只读资料库；读取昵称和头像需要对应账号正在电脑端登录。';
const WINDOWS_EMPTY = '未找到微信资料。请确认已在电脑端登录微信，或手动选择微信数据所在目录。';
const MAC_INTRO = '已从这台 Mac 上解密好的资料里查找账号。选择后建立本地只读资料库，并显示会话。';
const MAC_EMPTY = '未找到已解密的微信资料。请确认账号在个人目录 Noteford/Data/data/decrypted 下，或手动选择该目录。';
const MAC_PERMISSION_MESSAGE = '先允许这些权限，再选择微信账号。';

function windowIconPath({ platform, packaged, resourcesPath, desktopRoot }) {
  if (platform === 'darwin') {
    const file = 'noteford-mac.png';
    return packaged ? path.join(resourcesPath, file) : path.join(desktopRoot, 'assets', file);
  }
  return packaged
    ? path.join(resourcesPath, 'noteford-book-v1.ico')
    : path.join(desktopRoot, 'assets', 'noteford.ico');
}

function trayIconPath({ platform, packaged, resourcesPath, desktopRoot }) {
  if (platform === 'darwin') {
    const file = 'notefordTemplate.png';
    return packaged ? path.join(resourcesPath, file) : path.join(desktopRoot, 'assets', file);
  }
  return windowIconPath({ platform, packaged, resourcesPath, desktopRoot });
}

function presentTray(platform, nativeImage, iconPath) {
  const image = nativeImage.createFromPath(iconPath);
  if (platform === 'darwin' && image && typeof image.setTemplateImage === 'function') {
    image.setTemplateImage(true);
  }
  return image;
}

function hideOnClose(platform, trayReady) {
  return platform === 'darwin' || !!trayReady;
}

function bindReopen(platform, app, getWindow) {
  if (platform !== 'darwin') return;
  app.on('activate', () => {
    const window = getWindow();
    if (!window) return;
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
  });
}

function trayMenuTemplate(platform) {
  if (platform === 'darwin') {
    return [
      { label: '打开', action: 'show' },
      { type: 'separator' },
      { label: '退出', action: 'quit' },
    ];
  }
  return [{ label: '退出', action: 'quit' }];
}

function headerBuffer(value) {
  if (Buffer.isBuffer(value)) return value;
  if (value instanceof Uint8Array) return Buffer.from(value);
  return Buffer.from(String(value || ''), 'utf8');
}

function isMachOHeader(value) {
  const buf = headerBuffer(value);
  if (buf.length < 4) return false;
  const magic = buf.readUInt32LE(0);
  if (magic === 0xFEEDFACF || magic === 0xFEEDFACE || magic === 0xCEFAEDFE || magic === 0xCFFAEDFE) return true;
  if (buf[0] === 0xCA && buf[1] === 0xFE && buf[2] === 0xBA && buf[3] === 0xBE) return true;
  if (buf[0] === 0xBE && buf[1] === 0xBA && buf[2] === 0xFE && buf[3] === 0xCA) return true;
  return false;
}

function isPythonServiceLauncher(value) {
  const text = headerBuffer(value).subarray(0, 512).toString('utf8');
  return text.startsWith('#!') || text.includes('python3 -m noteford.service') || text.includes('python -m noteford.service');
}

function serviceSpec({ platform, packaged, resourcesPath, python, projectRoot, exists = () => false, readFile }) {
  if (platform !== 'darwin') {
    if (packaged) {
      return {
        command: path.join(resourcesPath, 'service', 'noteford-service.exe'),
        argsPrefix: [],
        extraEnv: {},
      };
    }
    return {
      command: python || 'python',
      argsPrefix: ['-m', 'noteford.service'],
      extraEnv: {},
    };
  }
  if (!packaged) {
    return {
      command: python || 'python3',
      argsPrefix: ['-m', 'noteford.service'],
      extraEnv: {},
    };
  }
  const binary = path.join(resourcesPath || '', 'service', 'noteford-service');
  let head = null;
  if (exists(binary) && typeof readFile === 'function') {
    try { head = readFile(binary); } catch { head = null; }
  }
  if (!head || isPythonServiceLauncher(head) || !isMachOHeader(head)) {
    throw new Error('Noteford Mac 安装包缺少自包含的 noteford-service，已拒绝改用系统 python3。');
  }
  return { command: binary, argsPrefix: [], extraEnv: {} };
}

function runtimeRoot({
  packaged,
  portableLayout,
  executableRoot,
  localAppData,
  userData,
  projectRoot,
  platform,
  home,
  exists = () => false,
}) {
  if (platform === 'darwin' && !portableLayout) {
    const macData = path.join(home, 'Noteford', 'Data');
    return packaged ? macData : path.join(projectRoot, 'Data');
  }
  if (!packaged) return projectRoot;
  if (portableLayout) return path.dirname(executableRoot);
  return path.join(localAppData || userData, 'Noteford');
}

function discoveryDirectory({ platform, localAppData, home, appData }) {
  if (platform === 'darwin') {
    return path.join(home, 'Library', 'Application Support', 'Noteford');
  }
  return path.join(localAppData || appData, 'Noteford');
}

function shouldPublishDiscovery({ smoke }) {
  return !smoke;
}

function permissionMarkerPath(directory) {
  return path.join(directory, 'permissions-requested.json');
}

function decryptedArchiveRoot(home) {
  return path.join(home, 'Noteford', 'Data', 'data', 'decrypted');
}

function wechatContainer(home) {
  return path.join(home, 'Library', 'Containers', 'com.tencent.xinWeChat');
}

function permissionsPending({ platform, requiredGranted = false }) {
  return platform === 'darwin' && !requiredGranted;
}

function fullDiskProbePaths(home) {
  // Read only the protected database header, never chat files or directory
  // listings. Container-specific access does not prove Full Disk Access.
  return [
    {
      id: 'tcc',
      kind: 'file',
      proves: true,
      path: path.join(home, 'Library', 'Application Support', 'com.apple.TCC', 'TCC.db'),
    },
    {
      // Newer macOS can relocate the per-user database into ProtectedSystem.
      // The system database remains protected by Full Disk Access too.
      id: 'tcc_system',
      kind: 'file',
      proves: true,
      path: '/Library/Application Support/com.apple.TCC/TCC.db',
    },
  ];
}

function readFullDiskTarget(target, io) {
  try {
    if (target.kind === 'file') {
      const opened = io.openSync(target.path, 'r');
      try {
        const buffer = Buffer.alloc(16);
        const bytes = io.readSync(opened, buffer, 0, buffer.length, 0);
        return guide.fullDiskProbeResult({ kind: 'file', bytes });
      } finally {
        try { io.closeSync(opened); } catch { /* already closed */ }
      }
    }
    return 'error'; // Directory enumeration is never permission proof.
  } catch (error) {
    return guide.fullDiskProbeResult({ kind: target.kind, error });
  }
}

function collectFullDiskReads(home, io) {
  return fullDiskProbePaths(home).map(target => ({
    id: target.id,
    proves: target.proves !== false,
    result: readFullDiskTarget(target, io),
  }));
}

function hostProfile({ platform, permissionsPending: pending = false }) {
  if (platform === 'darwin') {
    return {
      platform,
      onboarding: {
        intro: MAC_INTRO,
        empty: MAC_EMPTY,
        permissions: {
          pending: !!pending,
          autoRequest: false,
          title: '需要权限',
          message: MAC_PERMISSION_MESSAGE,
        },
      },
    };
  }
  return {
    platform,
    onboarding: {
      intro: WINDOWS_INTRO,
      empty: WINDOWS_EMPTY,
      permissions: { pending: false, autoRequest: false, title: '', message: '' },
    },
  };
}

const FULL_DISK_SETTINGS = guide.SETTINGS_URLS.full_disk;

module.exports = {
  WINDOWS_INTRO,
  WINDOWS_EMPTY,
  windowIconPath,
  trayIconPath,
  presentTray,
  hideOnClose,
  bindReopen,
  trayMenuTemplate,
  isMachOHeader,
  isPythonServiceLauncher,
  serviceSpec,
  runtimeRoot,
  discoveryDirectory,
  shouldPublishDiscovery,
  permissionMarkerPath,
  decryptedArchiveRoot,
  wechatContainer,
  permissionsPending,
  fullDiskProbePaths,
  readFullDiskTarget,
  collectFullDiskReads,
  hostProfile,
  FULL_DISK_SETTINGS,
  ...guide,
};

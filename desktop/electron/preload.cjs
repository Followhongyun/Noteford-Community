'use strict';

const { contextBridge, ipcRenderer } = require('electron');

let workspaceReady;
const invokeScoped = async (channel,...args) => {
  const scope=await (workspaceReady ||= ipcRenderer.invoke('noteford:workspace-ready'));
  return ipcRenderer.invoke(channel,{workspaceEpoch:scope.epoch},...args);
};
const api = {
  platform: process.platform,
  ...(process.platform === 'darwin' ? {nativeCorners: input => ipcRenderer.invoke('noteford:native-corners', input)} : {}),
  hostProfile: () => invokeScoped('noteford:host-profile'),
  permissionGuide: () => invokeScoped('noteford:permission-guide'),
  restartForPermissions: () => invokeScoped('noteford:permission-restart'),
  onPermissionState: callback => {
    const listener = (_event, guide) => callback(guide);
    ipcRenderer.on('noteford:permission-state', listener);
    return () => ipcRenderer.removeListener('noteford:permission-state', listener);
  },
  grantPermission: input => invokeScoped('noteford:permission-grant', input),
  completePermissionGuide: () => invokeScoped('noteford:permission-complete'),
  autoCaptureStatus: () => invokeScoped('noteford:auto-capture-status'),
  autoCaptureStart: input => invokeScoped('noteford:auto-capture-start', input || {}),
  mcpConnectionStatus: () => invokeScoped('noteford:mcp-connection-status'),
  configureMcpConnection: input => invokeScoped('noteford:mcp-connection-configure', input),
  mcpConnections: () => invokeScoped('noteford:mcp-list'),
  ...(process.platform === 'darwin' ? {
    macAiStatus: () => invokeScoped('noteford:mac-ai-status'),
    configureMacAi: input => invokeScoped('noteford:mac-ai-configure', input),
    copyMacAiConnection: input => invokeScoped('noteford:mac-ai-copy', input),
  } : {}),
  createMcpConnection: input => invokeScoped('noteford:mcp-create', input),
  revokeMcpConnection: input => invokeScoped('noteford:mcp-revoke', input),
  copyMcpConfig: input => invokeScoped('noteford:mcp-copy', input),
  exportMcpPlugin: input => invokeScoped('noteford:mcp-export', input),
  skillInstallCommand: input => invokeScoped('noteford:skill-install-command', input),
  copyMcpPrompt: () => invokeScoped('noteford:mcp-copy-prompt'),
  mcpPairBindings: () => invokeScoped('noteford:mcp-pair-bindings'),
  revokeMcpPairBinding: input => invokeScoped('noteford:mcp-pair-revoke', input),
  remoteMcpStatus: () => invokeScoped('noteford:remote-mcp-status'),
  configureRemoteMcp: input => invokeScoped('noteford:remote-mcp-configure', input),
  remoteMcpPairCode: () => invokeScoped('noteford:remote-mcp-pair-code'),
  remoteMcpPending: () => invokeScoped('noteford:remote-mcp-pending'),
  remoteMcpApprove: input => invokeScoped('noteford:remote-mcp-approve', input),
  remoteMcpDeny: input => invokeScoped('noteford:remote-mcp-deny', input),
  remoteMcpGrants: () => invokeScoped('noteford:remote-mcp-grants'),
  remoteMcpRevoke: input => invokeScoped('noteford:remote-mcp-revoke', input),
  status: () => invokeScoped('noteford:status'),
  saveAutoMediaLimit: maxMb => invokeScoped('noteford:auto-media-limit', maxMb),
  settings: () => invokeScoped('noteford:settings'),
  saveAutoMediaLoading: enabled => invokeScoped('noteford:auto-media-loading', enabled),
  saveStickerParsing: enabled => invokeScoped('noteford:parse-stickers', enabled),
  saveSyncIntervalSeconds: seconds => invokeScoped('noteford:sync-interval-seconds', seconds),
  saveSyncInterval: minutes => invokeScoped('noteford:sync-interval', minutes),
  avatars: input => invokeScoped('noteford:avatars', input),
  mediaRange: input => invokeScoped('noteford:media-range', input),
  saveMediaRange: input => invokeScoped('noteford:save-media-range', input),
  saveMediaWindow: input => invokeScoped('noteford:save-media-window', input),
  conversations: input => invokeScoped('noteford:conversations', input),
  content: input => invokeScoped('noteford:content', input),
  messages: input => invokeScoped('noteford:messages', input),
  mediaProgress: input => invokeScoped('noteford:media-progress', input),
  recheckMedia: input => invokeScoped('noteford:media-recheck', input),
  groupMembers: input => invokeScoped('noteford:group-members', input),
  contacts: input => invokeScoped('noteford:contacts', input),
  search: input => invokeScoped('noteford:search', input),
  openExternal: input => invokeScoped('noteford:open-external', input),
  ...(process.platform === 'darwin' ? {saveMedia: input => invokeScoped('noteford:mac-save-media', input), revealMedia: input => invokeScoped('noteford:mac-reveal-media', input)} : {}),
  libraries: () => invokeScoped('noteford:libraries'),
  libraryCandidates: input => invokeScoped('noteford:library-candidates', input),
  registerLibraryCandidate: input => invokeScoped('noteford:library-register', input),
  switchLibrary: input => invokeScoped('noteford:library-switch', input),
  renameLibrary: input => invokeScoped('noteford:library-rename', input),
  discoverLibraries: input => invokeScoped('noteford:library-discover', input),
  pickLibraryRoot: () => invokeScoped('noteford:pick-library-root'),
  follow: input => invokeScoped('noteford:follow', input),
  pause: input => invokeScoped('noteford:pause', input),
  refresh: () => invokeScoped('noteford:refresh'),
  macWechat: input => invokeScoped('noteford:mac-wechat', input || {}),

};

// A sandboxed preload cannot require local files. Keep this bridge self-contained.
if (process.platform === 'darwin') {
  let libraryId = null;
  let generation = 0;
  let selecting = false;
  let initial = null;
  const scope = async () => {
    if (libraryId !== null) return libraryId;
    if (!initial) initial = invokeScoped('noteford:libraries').then(rows => {
      libraryId = (rows.find(row => row.is_current) || rows[0])?.id || '';
      return libraryId;
    }).finally(() => { initial = null; });
    return initial;
  };
  const names = {status:'status', conversations:'conversations', content:'content', messages:'messages', groupMembers:'group-members', contacts:'contacts', search:'search', follow:'follow', pause:'pause', refresh:'refresh', avatars:'avatars', mediaProgress:'media-progress', recheckMedia:'media-recheck', mediaRange:'media-range', saveMediaRange:'save-media-range'};
  for (const [name, channel] of Object.entries(names)) api[name] = async input => {
    const version = generation;
    if (selecting) throw new Error('正在切换账号');
    const id = await scope();
    if (selecting || version !== generation) throw new Error('账号已切换');
    const result = await invokeScoped('noteford:mac-account-request', {channel: `noteford:${channel}`, library_id: id, input});
    if (version !== generation) throw new Error('账号已切换');
    return result;
  };
  api.switchLibrary = async input => {
    if (selecting) throw new Error('正在切换账号');
    selecting = true;
    generation++;
    try {
      // Let an initial lookup settle before replacing its captured account.
      if (initial) await initial;
      const result = await invokeScoped('noteford:library-switch', input);
      libraryId = result.library.id;
      return result;
    } catch (error) {
      libraryId = null;
      throw error;
    } finally { selecting = false; }
  };
  api.macAccounts = () => invokeScoped('noteford:mac-accounts');
  api.configureMacAccounts = input => invokeScoped('noteford:mac-accounts-configure', input);
  api.cancelMacAccount = () => invokeScoped('noteford:mac-accounts-cancel');
  api.connectMacAccount = input => invokeScoped('noteford:mac-accounts-connect', input);
}
contextBridge.exposeInMainWorld('noteford', api);

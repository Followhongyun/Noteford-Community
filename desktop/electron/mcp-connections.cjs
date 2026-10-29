'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function createMcpConnections({ runtimeRoot, command, argsPrefix = [], env = {}, pluginTemplate }) {
  const policyFile = path.join(runtimeRoot, 'data', 'mcp_access.json');
  function read() {
    if (!fs.existsSync(policyFile)) return { version: 1, clients: {} };
    const value = JSON.parse(fs.readFileSync(policyFile, 'utf8').replace(/^\uFEFF/, ''));
    if (value.version !== 1 || !value.clients || typeof value.clients !== 'object' || Array.isArray(value.clients)) throw new Error('AI 授权文件无效，请保留文件并检查');
    return value;
  }
  function write(value) {
    fs.mkdirSync(path.dirname(policyFile), { recursive: true });
    const temporary = `${policyFile}.${crypto.randomUUID()}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify(value, null, 2), { flag: 'wx' });
    fs.renameSync(temporary, policyFile);
  }
  function list() {
    return Object.entries(read().clients).map(([id, value]) => ({ id, ...value }));
  }
  function create({ label, library_id, conversations = [], scope = 'selected', allow_sync = false }) {
    if (typeof label !== 'string' || !label.trim() || label.length > 80 || typeof library_id !== 'string' || !library_id ||
        !['selected', 'all_synced', 'all_indexed'].includes(scope) || !Array.isArray(conversations) ||
        (scope === 'selected' && (conversations.length < 1 || conversations.length > 500)) ||
        conversations.some(item => typeof item !== 'string' || !item || item.length > 512)) throw new Error('请选择连接名称、账号和 1 至 500 个会话');
    const value = read();
    const id = crypto.randomUUID();
    value.clients[id] = { label: label.trim(), library_id, scope, allow_sync: allow_sync === true, conversations: [...new Set(conversations)], enabled: true, created_at: new Date().toISOString() };
    write(value);
    return { id, ...value.clients[id] };
  }
  function revoke(id) {
    const value = read();
    if (!Object.hasOwn(value.clients, id)) throw new Error('连接不存在');
    value.clients[id].enabled = false;
    write(value);
  }
  // pairToken: one-time token from local-pairing (never the permanent connection ID).
  function config(id, pairToken) {
    if (typeof pairToken !== 'string' || !pairToken.startsWith('nfp_')) throw new Error('缺少配对令牌');
    if (!Object.hasOwn(read().clients, id) || read().clients[id].enabled !== true) throw new Error('连接不存在或已撤销');
    return { mcpServers: { noteford: {
      command, args: [...argsPrefix, '--mcp', '--config', path.join(runtimeRoot, 'config.local.json'), '--pair', pairToken],
      env: { ...env, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' },
    } } };
  }
  function exportPlugin(id, parent, pairToken) {
    const value = config(id, pairToken);
    const destination = path.join(parent, `noteford-${id}`);
    // Unique connection directory; never overwrite an existing user folder.
    fs.mkdirSync(destination);
    // Copy entries into the reserved directory; cpSync may reject an existing root.
    for (const entry of fs.readdirSync(pluginTemplate)) {
      fs.cpSync(path.join(pluginTemplate, entry), path.join(destination, entry), {
        recursive: true, errorOnExist: true, force: false,
      });
    }
    fs.writeFileSync(path.join(destination, '.mcp.json'), JSON.stringify(value, null, 2), { flag: 'wx' });
    return destination;
  }
  return { list, create, revoke, config, exportPlugin };
}
module.exports = { createMcpConnections };

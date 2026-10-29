'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function createMcpConnectionState({ runtimeRoot }) {
  const file = path.join(runtimeRoot, 'data', 'mcp_connection.json');
  function status() {
    try {
      const value = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (value?.version !== 1 || typeof value.enabled !== 'boolean' || typeof value.generation !== 'string' || !value.generation) throw Error('invalid');
      return { enabled: value.enabled };
    } catch (error) {
      return { enabled: error.code === 'ENOENT' };
    }
  }
  function configure({ action } = {}) {
    if (!['disconnect', 'reconnect'].includes(action)) throw Error('无效的连接操作');
    const value = { version: 1, enabled: action === 'reconnect', generation: crypto.randomUUID() };
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const temp = `${file}.${crypto.randomUUID()}.tmp`;
    try {
      fs.writeFileSync(temp, JSON.stringify(value), { mode: 0o600, flag: 'wx' });
      fs.renameSync(temp, file);
    } finally { try { fs.unlinkSync(temp); } catch {} }
    return status();
  }
  return { status, configure };
}
module.exports = { createMcpConnectionState };

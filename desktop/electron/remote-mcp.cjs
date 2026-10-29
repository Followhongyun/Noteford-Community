'use strict';
// Owner-controlled remote MCP (streamable HTTP + OAuth). Off by default; runs only while Noteford runs.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');
const { spawn } = require('node:child_process');

function createRemoteMcp({ runtimeRoot, command, argsPrefix = [], env = {}, log = () => {}, assertActive = () => {} }) {
  const settingsFile = path.join(runtimeRoot, 'data', 'mcp_remote.json');
  let child = null;
  let adminSecret = null;
  let lastError = '';

  function settings() {
    const fallback = { version: 1, enabled: false, public_url: '', port: 8765, admin_port: 8766 };
    if (!fs.existsSync(settingsFile)) return fallback;
    try { return { ...fallback, ...JSON.parse(fs.readFileSync(settingsFile, 'utf8').replace(/^\uFEFF/, '')) }; } catch { return fallback; }
  }
  function validate(value) {
    if (value.enabled) {
      let url;
      try { url = new URL(value.public_url); } catch { throw new Error('请填写公网 HTTPS 地址'); }
      if (url.protocol !== 'https:' || url.pathname.replace(/\/$/, '') !== '' || url.search || url.hash) throw new Error('公网地址必须是 https://域名，不带路径');
    }
    for (const key of ['port', 'admin_port']) if (!Number.isInteger(value[key]) || value[key] < 1024 || value[key] > 65535) throw new Error('端口无效');
    if (value.port === value.admin_port) throw new Error('端口不能相同');
  }
  function save(input = {}) {
    assertActive();
    const value = { ...settings(), ...pick(input, ['enabled', 'public_url', 'port', 'admin_port']) };
    value.enabled = value.enabled === true;
    value.public_url = String(value.public_url || '').trim().replace(/\/$/, '');
    validate(value);
    fs.mkdirSync(path.dirname(settingsFile), { recursive: true });
    const tmp = `${settingsFile}.${crypto.randomUUID()}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(value, null, 2), { flag: 'wx' });
    fs.renameSync(tmp, settingsFile);
    return value;
  }
  function pick(obj, keys) { return Object.fromEntries(keys.filter(k => Object.hasOwn(obj, k)).map(k => [k, obj[k]])); }

  function start() {
    assertActive();
    const value = settings();
    if (!value.enabled || child) return status();
    validate(value);
    adminSecret = crypto.randomBytes(32).toString('hex');
    lastError = '';
    child = spawn(command, [...argsPrefix, '--mcp-remote', '--config', path.join(runtimeRoot, 'config.local.json'),
      '--public-url', value.public_url, '--port', String(value.port), '--admin-port', String(value.admin_port)], {
      env: { ...process.env, ...env, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1', NOTEFORD_REMOTE_ADMIN_SECRET: adminSecret },
      windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'],
    });
    child.stderr.on('data', chunk => { lastError = String(chunk).slice(-500); log('remote-mcp', lastError); });
    child.on('exit', code => { if (code) lastError = lastError || `远程服务退出（${code}）`; child = null; adminSecret = null; });
    return status();
  }
  function stop() {
    const previous = child;
    if (!previous) return Promise.resolve();
    adminSecret = null;
    return new Promise((resolve, reject) => {
      previous.once('exit', resolve);
      previous.kill();
      const force = setTimeout(() => previous.kill('SIGKILL'), 3000);
      const timeout = setTimeout(() => reject(Error('remote_mcp_still_running')), 6000);
      previous.once('exit', () => { clearTimeout(force); clearTimeout(timeout); });
    });
  }
  function status() {
    const value = settings();
    return { enabled: value.enabled, public_url: value.public_url, port: value.port, running: Boolean(child),
      mcp_url: value.public_url ? `${value.public_url}/mcp` : '', error: lastError };
  }
  async function configure(input) {
    await stop();
    const value = save(input);
    if (value.enabled) start();
    return status();
  }

  function admin(method, route, body) {
    assertActive();
    if (!child || !adminSecret) return Promise.reject(new Error('远程连接未开启'));
    const payload = body ? JSON.stringify(body) : '';
    return new Promise((resolve, reject) => {
      const req = http.request({ host: '127.0.0.1', port: settings().admin_port, path: route, method, timeout: 5000,
        headers: { 'x-noteford-admin': adminSecret, 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) } }, res => {
        let data = '';
        res.on('data', c => { data += c; });
        res.on('end', () => {
          let parsed = null;
          try { parsed = data ? JSON.parse(data) : null; } catch { /* fallthrough */ }
          if (res.statusCode >= 400) reject(new Error((parsed && parsed.error) || `HTTP ${res.statusCode}`)); else resolve(parsed);
        });
      });
      req.on('timeout', () => req.destroy(new Error('远程服务无响应')));
      req.on('error', reject);
      req.end(payload);
    });
  }
  return {
    settings, status, configure, start, stop,
    pairCode: () => admin('POST', '/admin/pair-code'),
    pending: () => admin('GET', '/admin/pending'),
    approve: (display_id, grant_client) => admin('POST', '/admin/approve', { display_id, grant_client }),
    deny: display_id => admin('POST', '/admin/deny', { display_id }),
    grants: () => admin('GET', '/admin/grants'),
    revoke: id => admin('POST', '/admin/revoke', { id }),
  };
}
module.exports = { createRemoteMcp };

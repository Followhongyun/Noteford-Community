'use strict';
// Desktop side of local stdio MCP pairing. Talks to `service --mcp-local-admin` (JSON over stdout).
const path = require('node:path');
const { execFile } = require('node:child_process');

function createLocalPairing({ runtimeRoot, command, argsPrefix = [], env = {}, platform = process.platform, execute = execFile }) {
  function run(action, value, stdin, extra) {
    const args = [...argsPrefix, '--mcp-local-admin', '--config', path.join(runtimeRoot, 'config.local.json'), action];
    if (platform === 'darwin' && action !== 'issue') args.splice(args.length - 1, 0, '--noninteractive-keychain');
    if (value !== undefined) args.push(String(value));
    if (extra !== undefined) args.push(String(extra));
    return new Promise((resolve, reject) => {
      const child = execute(command, args, {
        env: { ...process.env, ...env, PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' }, windowsHide: true, timeout: 20000,
      }, (error, stdout) => {
        let parsed = null;
        try { parsed = JSON.parse(String(stdout || '').trim()); } catch { /* handled below */ }
        if (parsed && parsed.ok) return resolve(parsed.result);
        if (parsed && parsed.error) return reject(new Error(describe(parsed.error)));
        reject(new Error(error ? '授权服务调用失败' : '授权服务返回无效'));
      });
      child.stdin.end(stdin ? `${stdin}\n` : '');
    });
  }
  function describe(code) {
    if (code === 'grant_not_enabled') return '该连接已撤销';
    if (code === 'request_not_awaiting_confirm') return '请求已过期，请让 AI 重新发起连接';
    if (code === 'binding_missing') return '授权不存在';
    if (code === 'keychain_access_required') return '现有 AI 连接需要钥匙串授权，请主动重新导出连接配置后重试。聊天同步不受影响。';
    return '操作失败';
  }
  return {
    issue: grantId => run('issue', grantId || '').then(result => result.token),
    approve: (requestId, grantClient) => run('approve', requestId, undefined, grantClient),
    deny: requestId => run('deny', requestId),
    bindings: () => run('bindings'),
    requests: () => run('requests'),
    review: requestId => run('review', requestId),
    revoke: bindingId => run('revoke', bindingId),
  };
}
module.exports = { createLocalPairing };

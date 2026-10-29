'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const { AsyncLocalStorage } = require('node:async_hooks');

function atomicJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = file + '.' + randomUUID() + '.tmp';
  try {
    fs.writeFileSync(tmp, JSON.stringify(value, null, 2), { flag: 'wx', mode: 0o600 });
    fs.renameSync(tmp, file);
  } finally { try { fs.unlinkSync(tmp); } catch {} }
}

function validateAccount(account) {
  if (!account || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(account.id)
      || typeof account.email !== 'string' || !account.email.includes('@')) throw Error('network');
  return account.id.toLowerCase();
}

class ProductWorkspaces {
  constructor(installationRoot, apiOrigin) {
    fs.mkdirSync(installationRoot, { recursive: true, mode: 0o700 });
    this.installation = fs.realpathSync(installationRoot);
    this.apiOrigin = apiOrigin;
    this.directory = path.join(this.installation, 'data/runtime/product-account');
    this.manifestFile = path.join(this.directory, 'workspaces.json');
    this.leaseFile = path.join(this.directory, 'active-workspace.json');
    this.context = new AsyncLocalStorage();
    this.active = null;
    this.lock();
    // Reserve pre-account data. It can only be claimed by a separately confirmed owner.
    if (!fs.existsSync(this.manifestFile)) atomicJson(this.manifestFile, {
      version: 1, apiOrigin, legacyOwnerEmail: null, legacyOwnerId: null,
    });
    atomicJson(path.join(this.installation, '.product-workspace.json'), {
      version: 1, installation: this.installation, workspace: 'legacy',
    });
  }
  lock() {
    this.active?.abort.abort();
    this.active = null;
    atomicJson(this.leaseFile, { version: 1, active: false });
  }
  assert(scope = this.context.getStore() || this.active) {
    if (!scope || scope !== this.active || scope.abort.signal.aborted) throw Error('账号已退出或切换，请重新登录。');
    return scope;
  }
  activate(account) {
    const id = account === null ? null : validateAccount(account);
    if (this.active) throw Error('workspace_busy');
    const manifest = JSON.parse(fs.readFileSync(this.manifestFile, 'utf8'));
    if (manifest.version !== 1 || manifest.apiOrigin !== this.apiOrigin) throw Error('storage');
    if (account && !manifest.legacyOwnerId && manifest.legacyOwnerEmail
        && account.email.trim().toLowerCase() === manifest.legacyOwnerEmail.trim().toLowerCase()) {
      manifest.legacyOwnerId = id;
      atomicJson(this.manifestFile, manifest);
    }
    const workspace = id === null ? 'local' : manifest.legacyOwnerId === id ? 'legacy'
      : createHash('sha256').update(this.apiOrigin + '\n' + id).digest('hex');
    const root = workspace === 'legacy' ? this.installation : path.join(this.installation, 'product-accounts', workspace);
    fs.mkdirSync(root, { recursive: true, mode: 0o700 });
    if (fs.realpathSync(root) !== root) throw Error('storage');
    const configFile = path.join(root, 'config.local.json');
    if (!fs.existsSync(configFile)) atomicJson(configFile, {
      project_root: root, wechat_roots: [],
      library_registry_path: path.join(root, 'data/library_registry.json'),
      allow_memory_rescan: true, auto_refresh_interval_seconds: 300,
    });
    const config = JSON.parse(fs.readFileSync(configFile, 'utf8'));
    if (fs.realpathSync(path.resolve(root, config.project_root || '.')) !== root) throw Error('storage');
    atomicJson(path.join(root, '.product-workspace.json'), { version: 1, installation: this.installation, workspace });
    const scope = { root, workspace, accountId: id, epoch: randomUUID(), abort: new AbortController() };
    atomicJson(this.leaseFile, { version: 1, active: true, root, workspace, accountId: id, epoch: scope.epoch,
      pid: process.pid, appExecutable: process.execPath, processStartedAtMs: Date.now() - process.uptime() * 1000 });
    this.active = scope;
    return scope;
  }
  run(scope, callback) {
    this.assert(scope);
    return this.context.run(scope, callback);
  }
  guard(scope, object) {
    return new Proxy(object, { get: (target, key) => typeof target[key] === 'function'
      ? (...args) => { this.assert(scope); return target[key](...args); } : target[key] });
  }
}
module.exports = { ProductWorkspaces, validateAccount, atomicJson };

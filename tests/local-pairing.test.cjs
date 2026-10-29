'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createLocalPairing } = require('../desktop/electron/local-pairing.cjs');
const { createMcpConnections } = require('../desktop/electron/mcp-connections.cjs');

const repo = path.resolve(__dirname, '..');
const python = process.env.NOTEFORD_PYTHON || path.join(repo, '.venv-build', 'Scripts', 'python.exe');

test('Mac background pairing checks cannot prompt; explicit export can and Windows args stay unchanged', async () => {
  for (const platform of ['darwin', 'win32']) {
    const calls = [];
    const pairing = createLocalPairing({runtimeRoot: '/app', command: '/service', platform,
      execute(command, args, options, done) {
        calls.push(args);
        done(null, JSON.stringify({ok:true,result:{token:'fixture'}}));
        return {stdin:{end(){}}};
      }});
    await pairing.requests(); await pairing.bindings(); await pairing.issue('id');
    assert.equal(calls[0].includes('--noninteractive-keychain'), platform === 'darwin');
    assert.equal(calls[1].includes('--noninteractive-keychain'), platform === 'darwin');
    assert.equal(calls[2].includes('--noninteractive-keychain'), false);
  }
});

test('exported config carries only a one-time pairing token and the admin CLI works', { skip: process.platform !== 'win32' || !fs.existsSync(python) }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-pair-'));
  try {
    fs.writeFileSync(path.join(root, 'config.local.json'), JSON.stringify({ project_root: root, wechat_roots: [] }));
    const manager = createMcpConnections({ runtimeRoot: root, command: python, pluginTemplate: path.join(repo, 'desktop', 'mcp-plugin') });
    const pairing = createLocalPairing({ runtimeRoot: root, command: python, argsPrefix: ['-m', 'noteford.service'], env: { PYTHONPATH: path.join(repo, 'src') } });
    const grant = manager.create({ label: 'Codex', library_id: 'lib', scope: 'all_synced' });
    const token = await pairing.issue(grant.id);
    assert.match(token, /^nfp_/);
    assert.match(await pairing.issue(), /^nfp_/); // unbound token for 复制提示词
    const folder = manager.exportPlugin(grant.id, root, token);
    const exported = fs.readFileSync(path.join(folder, '.mcp.json'), 'utf8');
    const args = JSON.parse(exported).mcpServers.noteford.args;
    assert.deepEqual(args.slice(-2), ['--pair', token]);
    assert.ok(!args.includes('--client') && !exported.includes(grant.id));
    assert.deepEqual(await pairing.bindings(), []);
    const store = fs.readFileSync(path.join(root, 'data', 'mcp_local.store'));
    assert.ok(!store.includes(Buffer.from(token)) && !store.includes(Buffer.from('Codex')));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

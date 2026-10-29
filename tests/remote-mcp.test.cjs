'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createRemoteMcp } = require('../desktop/electron/remote-mcp.cjs');

const repo = path.resolve(__dirname, '..');
const python = process.env.NOTEFORD_PYTHON || path.join(repo, '.venv-build', 'Scripts', 'python.exe');

test('remote MCP is off by default, validates settings and drives the admin API', { skip: process.platform !== 'win32' || !fs.existsSync(python) }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-remote-'));
  fs.writeFileSync(path.join(root, 'config.local.json'), JSON.stringify({ project_root: root, wechat_roots: [] }));
  const port = 20000 + Math.floor(Math.random() * 20000);
  const remote = createRemoteMcp({ runtimeRoot: root, command: python, argsPrefix: ['-m', 'noteford.service'], env: { PYTHONPATH: path.join(repo, 'src') } });
  try {
    assert.equal(remote.status().enabled, false);
    assert.equal(remote.start().running, false);
    await assert.rejects(remote.pairCode(), /未开启/);
    await assert.rejects(remote.configure({ enabled: true, public_url: 'http://insecure.example' }), /https/);
    await assert.rejects(remote.configure({ enabled: true, public_url: 'https://x.example/path' }), /https/);
    const status = await remote.configure({ enabled: true, public_url: 'https://nf.example', port, admin_port: port + 1 });
    assert.equal(status.running, true);
    assert.equal(status.mcp_url, 'https://nf.example/mcp');
    let code;
    for (let i = 0; i < 60 && !code; i++) {
      try { code = await remote.pairCode(); } catch { await new Promise(r => setTimeout(r, 500)); }
    }
    assert.match(code.code, /^[A-HJ-NP-Z2-9]{8}$/);
    assert.deepEqual(await remote.pending(), []);
    assert.deepEqual(await remote.grants(), []);
    const saved = fs.readFileSync(path.join(root, 'data', 'mcp_remote.json'), 'utf8');
    assert.ok(!saved.includes(code.code));
    const store = fs.readFileSync(path.join(root, 'data', 'mcp_remote.store'));
    assert.ok(!store.includes(Buffer.from(code.code)));
  } finally {
    remote.stop();
    await new Promise(r => setTimeout(r, 500));
    fs.rmSync(root, { recursive: true, force: true });
  }
});

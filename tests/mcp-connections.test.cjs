const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createMcpConnections } = require('../desktop/electron/mcp-connections.cjs');

test('official plugin export keeps client scope and revocation', t => {
  const base = path.resolve('.pytest-tmp'); fs.mkdirSync(base, { recursive: true });
  const root = fs.mkdtempSync(path.join(base, 'mcp-node-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const pluginTemplate = path.resolve('desktop/mcp-plugin');
  const manager = createMcpConnections({ runtimeRoot: root, command: 'C:\\Program Files\\Noteford\\service.exe', pluginTemplate });
  assert.deepEqual(manager.list(), []);
  assert.throws(() => manager.create({ label: 'test', library_id: 'lib', conversations: [] }));
  const grant = manager.create({ label: '测试 AI', library_id: 'lib', conversations: ['chat-a','chat-a'] });
  assert.deepEqual(grant.conversations, ['chat-a']);
  const folder = manager.exportPlugin(grant.id, root, 'nfp_test');
  const configBytes = fs.readFileSync(path.join(folder, '.mcp.json'));
  const config = JSON.parse(configBytes);
  assert.equal(config.mcpServers['noteford'].command, 'C:\\Program Files\\Noteford\\service.exe');
  assert.deepEqual(config.mcpServers['noteford'].args, ['--mcp', '--config', path.join(root,'config.local.json'), '--pair', 'nfp_test']);
  assert.throws(() => manager.config(grant.id));
  const templateEntries = fs.readdirSync(pluginTemplate, { recursive: true }).sort();
  assert.deepEqual(fs.readdirSync(folder, { recursive: true }).sort(), [...templateEntries, '.mcp.json'].sort());
  for (const entry of templateEntries) {
    if (fs.statSync(path.join(pluginTemplate, entry)).isFile()) {
      assert.deepEqual(fs.readFileSync(path.join(folder, entry)), fs.readFileSync(path.join(pluginTemplate, entry)), entry);
    }
  }
  assert.throws(() => manager.exportPlugin(grant.id, root, 'nfp_retry'), { code: 'EEXIST' });
  assert.deepEqual(fs.readFileSync(path.join(folder, '.mcp.json')), configBytes);
  manager.revoke(grant.id);
  assert.equal(manager.list()[0].enabled, false);
  assert.throws(() => manager.config(grant.id, 'nfp_test'));
  const anotherParent = path.join(root, 'after-revoke');
  fs.mkdirSync(anotherParent);
  assert.throws(() => manager.exportPlugin(grant.id, anotherParent, 'nfp_test'), /连接不存在或已撤销/);
  assert.deepEqual(fs.readdirSync(anotherParent), []);
});

for (const kind of ['directory', 'file']) {
  test(`plugin export preserves an existing user ${kind}`, t => {
    const base = path.resolve('.pytest-tmp'); fs.mkdirSync(base, { recursive: true });
    const root = fs.mkdtempSync(path.join(base, 'mcp-existing-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const manager = createMcpConnections({ runtimeRoot: root, command: 'service.exe', pluginTemplate: path.resolve('desktop/mcp-plugin') });
    const grant = manager.create({ label: 'test', library_id: 'lib', conversations: ['chat-a'] });
    const destination = path.join(root, `noteford-${grant.id}`);
    if (kind === 'directory') fs.mkdirSync(destination);
    const existingFile = kind === 'directory' ? path.join(destination, 'keep.txt') : destination;
    fs.writeFileSync(existingFile, 'existing user content');

    assert.throws(() => manager.exportPlugin(grant.id, root, 'nfp_test'), { code: 'EEXIST' });
    assert.equal(fs.readFileSync(existingFile, 'utf8'), 'existing user content');
    if (kind === 'directory') assert.deepEqual(fs.readdirSync(destination), ['keep.txt']);
  });
}

 test('whole library scope is explicit and preserves revoke', () => {
  const root = fs.mkdtempSync(path.join(path.resolve('.pytest-tmp'), 'mcp-all-'));
  const manager = createMcpConnections({ runtimeRoot: root, command: 'service.exe' });
  const entry = manager.create({label: 'AI', library_id: 'lib', scope: 'all_synced'});
  assert.equal(entry.scope, 'all_synced');
  assert.deepEqual(entry.conversations, []);
  assert.throws(() => manager.create({label:'AI',library_id:'lib',scope:'unknown'}));
  manager.revoke(entry.id);
  assert.throws(() => manager.config(entry.id, 'nfp_test'));
 });

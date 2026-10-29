const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createMacAiAccess } = require('../electron/mac-ai-access.cjs');

test('Mac direct access is opt-in, account-bound, revocable and copies credential-free transports', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-ai-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const access = createMacAiAccess({ runtimeRoot: root, command: '/App/service', cliPath: '/App/cli/noteford-ai' });
  assert.equal(access.status().enabled, false);
  assert.throws(() => access.configure({ enabled: true, library_id: 'a' }, { id: 'b', initialized: true }));
  assert.throws(() => access.configure({ enabled: true, library_id: 'a' }, { id: 'a', initialized: false }));
  assert.deepEqual(access.configure({ enabled: true, library_id: 'a' }, { id: 'a', initialized: true }), { enabled: true, library_id: 'a', scope: 'all_indexed', allow_sync: true });
  assert.deepEqual(access.config(), { mcpServers: { noteford: { command: '/App/service', args: ['--mcp-auto', '--client-name', 'MCP Client'] } } });
  assert.match(access.cliInstructions(), /--tool list_libraries/);
  assert.equal(access.configure({ enabled: false }, null).enabled, false);
  fs.writeFileSync(path.join(root, 'data/mac_ai_access.json'), 'null');
  assert.equal(access.status().enabled, false);
});

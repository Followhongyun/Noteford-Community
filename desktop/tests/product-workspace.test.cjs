const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { ProductWorkspaces, atomicJson } = require('../electron/product-workspace.cjs');

const a = { id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', email: 'owner@example.test' };
const b = { id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', email: 'other@example.test' };
function setup(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-workspaces-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  atomicJson(path.join(root, 'config.local.json'), { project_root: root, wechat_roots: ['/original/wechat'] });
  fs.mkdirSync(path.join(root, 'libraries')); fs.writeFileSync(path.join(root, 'libraries', 'keep'), 'owner data');
  const manager = new ProductWorkspaces(root, 'https://accounts.example');
  atomicJson(manager.manifestFile, {version: 1, apiOrigin: manager.apiOrigin, legacyOwnerEmail: a.email, legacyOwnerId: null});
  return manager;
}
test('QA login cannot claim legacy data; stable identities preserve A → B → A data', t => {
  const w = setup(t), first = w.activate(b);
  assert.notEqual(first.root, w.installation);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(first.root, 'config.local.json'))).wechat_roots, []);
  assert.equal(fs.existsSync(path.join(first.root, 'libraries')), false);
  w.lock(); const owner = w.activate(a);
  assert.equal(owner.root, w.installation);
  assert.equal(fs.readFileSync(path.join(owner.root, 'libraries/keep'), 'utf8'), 'owner data');
  w.lock(); assert.equal(w.activate(b).root, first.root);
  w.lock(); assert.equal(w.activate({...a, email: 'changed@example.test'}).root, owner.root);
  assert.equal(fs.readFileSync(path.join(owner.root, 'libraries/keep'), 'utf8'), 'owner data');
});
test('logout cancels old requests and continuations cannot bind to a new account', async t => {
  const w = setup(t), first = w.activate(a);
  let finish;
  const delayed = w.run(first, async () => {
    await new Promise(resolve => finish = resolve);
    return w.assert().root;
  });
  const protectedStore = w.guard(first, { write() { throw Error('must not reach writer'); } });
  w.lock(); assert.equal(first.abort.signal.aborted, true);
  assert.throws(() => w.assert());
  const second = w.activate(b); finish();
  await assert.rejects(delayed, /退出或切换/);
  assert.throws(() => protectedStore.write(), /退出或切换/);
  assert.equal(w.assert(), second);
});
test('restart invalidates the previous lease; corrupt owner map and unsafe identity fail closed', t => {
  const w = setup(t); const scope = w.activate(a);
  new ProductWorkspaces(w.installation, w.apiOrigin);
  assert.equal(JSON.parse(fs.readFileSync(w.leaseFile)).active, false);
  w.lock(); assert.throws(() => w.activate({...b, id: '../legacy'}));
  atomicJson(w.manifestFile, {version: 1, apiOrigin: 'https://other.example', legacyOwnerId: b.id});
  assert.throws(() => w.activate(b));
  assert.equal(fs.readFileSync(path.join(scope.root, 'libraries/keep'), 'utf8'), 'owner data');
});
test('unclaimed legacy data and symlinked profile roots are never adopted', t => {
  const w = setup(t);
  atomicJson(w.manifestFile, {version: 1, apiOrigin: w.apiOrigin, legacyOwnerEmail: null});
  const scope = w.activate(a); assert.notEqual(scope.root, w.installation);
  w.lock(); fs.rmSync(scope.root, {recursive: true}); fs.symlinkSync(w.installation, scope.root, 'dir');
  assert.throws(() => w.activate(a), /storage/);
});

test('local workspace persists independently and cannot adopt account or legacy data', t => {
  const w = setup(t), local = w.activate(null);
  assert.notEqual(local.root, w.installation);
  fs.writeFileSync(path.join(local.root, 'keep'), 'local data');
  w.lock(); const owner = w.activate(a);
  assert.notEqual(owner.root, local.root);
  assert.equal(fs.existsSync(path.join(owner.root, 'keep')), false);
  w.lock(); const restored = w.activate(null);
  assert.equal(restored.root, local.root);
  assert.equal(fs.readFileSync(path.join(restored.root, 'keep'), 'utf8'), 'local data');
});

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { createHash } = require('node:crypto');
const { verifyRelease, gatedAuthenticator } = require('../../distribution/server.cjs');

test('release startup rejects incomplete, altered and escaping artifacts', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'noteford-origin-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const artifacts = [];
  for (const [name, text] of [['latest-mac.yml', 'version: 0.1.1'], ['Noteford-0.1.1-arm64.zip', 'fixture']]) {
    await fs.writeFile(path.join(root, name), text);
    artifacts.push({ name, size: Buffer.byteLength(text), sha512: createHash('sha512').update(text).digest('hex') });
  }
  const release = { version: '0.1.1', artifacts };
  const save = () => fs.writeFile(path.join(root, 'release.json'), JSON.stringify(release));
  await save(); assert.equal((await verifyRelease(root)).version, '0.1.1');
  await fs.writeFile(path.join(root, artifacts[1].name), 'changed');
  await assert.rejects(verifyRelease(root), /checksum/);
  await fs.unlink(path.join(root, artifacts[1].name));
  await fs.symlink('/etc/hosts', path.join(root, artifacts[1].name));
  await assert.rejects(verifyRelease(root), /path/);
  release.artifacts = artifacts.slice(0, 1); await save();
  await assert.rejects(verifyRelease(root), /manifest/);
});

test('canary gate stays closed until promotion and never overrides invitation revocation', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'noteford-access-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const accessFile = path.join(root, 'access.json');
  let account = { id: 'canary-account', notefordAccess: true };
  let checks = 0;
  const auth = gatedAuthenticator({ accessFile, authenticate: async () => { checks++; return account; } });
  await assert.rejects(auth({}));
  const hash = createHash('sha256').update('https://api.muxuno.com\ncanary-account').digest('hex');
  await fs.writeFile(accessFile, JSON.stringify({ mode: 'canary', accountHashes: [hash] }));
  assert.equal((await auth({})).id, 'canary-account');
  account = { id: 'other-account', notefordAccess: true }; assert.equal(await auth({}), null);
  await fs.writeFile(accessFile, JSON.stringify({ mode: 'entitled' }));
  assert.equal((await auth({})).id, 'other-account');
  account.notefordAccess = false; assert.equal(await auth({}), null);
  account = null; assert.equal(await auth({}), null);
  assert.equal(checks, 6);
});


test('optional differential sidecars preserve base manifest and reject wrong scope, corruption and duplicates', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'noteford-sidecar-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const artifact = async (name, text) => {
    await fs.writeFile(path.join(root, name), text);
    return { name, size: Buffer.byteLength(text), sha512: createHash('sha512').update(text).digest('hex') };
  };
  const base = { version:'0.1.4', artifacts: [
    await artifact('latest-mac.yml','version: 0.1.4'),
    await artifact('Noteford-0.1.4-arm64.zip','unchanged signed zip'),
  ] };
  const raw=JSON.stringify(base);
  await fs.writeFile(path.join(root,'release.json'),raw);
  const item=await artifact('Noteford-0.1.3-arm64.zip.blockmap','old map');
  const side={version:'0.1.4',artifacts:[item]};
  const save=()=>fs.writeFile(path.join(root,'blockmaps.json'),JSON.stringify(side));
  await save();assert.equal((await verifyRelease(root)).files.length,3);
  assert.equal(await fs.readFile(path.join(root,'release.json'),'utf8'),raw);
  side.version='0.1.3';await save();await assert.rejects(verifyRelease(root),/blockmap/);
  side.version='0.1.4';side.artifacts=[item,item];await save();await assert.rejects(verifyRelease(root),/artifact/i);
  side.artifacts=[{...item,name:'other.json'}];await save();await assert.rejects(verifyRelease(root),/blockmap/);
  side.artifacts=[{...item,size:1024*1024+1}];await save();await assert.rejects(verifyRelease(root),/blockmap/);
  side.artifacts=[item];await save();await fs.writeFile(path.join(root,item.name),'bad map');
  await assert.rejects(verifyRelease(root),/checksum/);
});

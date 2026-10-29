const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const preload = path.resolve(__dirname, '../scripts/node-trust.cjs');
const bad = '86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b';
const probe = `const tls=require('node:tls'),c=require('node:crypto');const roots=tls.getCACertificates('default');console.log(JSON.stringify({count:roots.length,bad:roots.some(p=>c.createHash('sha256').update(new c.X509Certificate(p).raw).digest('hex')==='${bad}')}))`;

test('reviewed default CA replacement reaches build children and workers', () => {
  const code = `require(${JSON.stringify(preload)});
    const {spawnSync}=require('node:child_process'),{Worker}=require('node:worker_threads');
    ${probe};
    const child=spawnSync(process.execPath,['-e',${JSON.stringify(probe)}],{encoding:'utf8',env:process.env});
    if(child.status!==0)throw Error(child.stderr);console.log(child.stdout.trim());
    const worker=new Worker(${JSON.stringify(probe)},{eval:true,stdout:true});worker.stdout.pipe(process.stdout);`;
  // npm/package scripts must preload at Node startup. Setting NODE_OPTIONS
  // after startup alone does not change the defaults inherited by workers.
  const result = spawnSync(process.execPath, ['--require', preload, '-e', code], {encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  const states = result.stdout.trim().split('\n').map(x => JSON.parse(x));
  assert.equal(states.length, 3);
  for (const state of states) {
    assert.equal(state.bad, false);
    assert.ok(state.count > 100);
  }
});

test('tampered build CA data fails closed before any installer can run', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'noteford-build-ca-'));
  try {
    fs.mkdirSync(path.join(root, 'scripts'));
    fs.mkdirSync(path.join(root, 'security'));
    fs.copyFileSync(preload, path.join(root, 'scripts/node-trust.cjs'));
    fs.copyFileSync(path.join(__dirname, '../security/ca-policy.json'), path.join(root, 'security/ca-policy.json'));
    fs.writeFileSync(path.join(root, 'security/cacert.pem'), 'tampered CA bytes');
    const result = spawnSync(process.execPath, [path.join(root, 'scripts/node-trust.cjs')], {encoding: 'utf8'});
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Build CA bundle verification failed/);
  } finally { fs.rmSync(root, {recursive: true, force: true}); }
});

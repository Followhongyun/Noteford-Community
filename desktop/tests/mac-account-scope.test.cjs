'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../electron/preload.cjs'), 'utf8');
function bridge(platform, invoke) {
  let api;
  vm.runInNewContext(source, {process: {platform}, require: () => ({contextBridge: {exposeInMainWorld: (_name, value) => { api = value; }}, ipcRenderer: {invoke}})});
  return api;
}
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
test('Mac requests bind their account and discard a late result after switching', async () => {
  const old = deferred(); const switching = deferred(); const started = deferred();
  const calls = [];
  const api = bridge('darwin', async (channel, _workspace, input) => {
    calls.push({channel, input});
    if (channel === 'noteford:libraries') return [{id:'A', is_current: true}];
    if (channel === 'noteford:library-switch') return switching.promise;
    if (input.library_id === 'A') { started.resolve(); return old.promise; }
    return {account: input.library_id};
  });
  const first = api.messages({conversation: 'same'});
  await started.promise;
  const changed = api.switchLibrary({library_id:'B'});
  await assert.rejects(api.follow({username:'same'}), /正在切换/);
  switching.resolve({library:{id:'B'}}); await changed;
  old.resolve({account:'A'});
  await assert.rejects(first, /账号已切换/);
  assert.equal((await api.messages({conversation:'same'})).account, 'B');
  await api.follow({username:'same'});
  assert.equal(calls.at(-1).input.library_id, 'B');
  assert.equal(calls.filter(c => c.channel === 'noteford:mac-account-request').length, 3);
});
test('Windows IPC remains unchanged', async () => {
  const calls = [];
  const api = bridge('win32', async (...args) => { calls.push(args); return {}; });
  await api.messages({conversation:'same'}); await api.switchLibrary({library_id:'B'});
  assert.equal(calls[0][0], 'noteford:messages');
  assert.equal(calls[1][0], 'noteford:library-switch');
  assert.equal(api.macAccounts, undefined);
});
test('Failed switch re-resolves actual account before another read', async () => {
  let current = 'A';
  const api = bridge('darwin', async (channel, _workspace, input) => {
    if (channel === 'noteford:libraries') return [{id:current, is_current:true}];
    if (channel === 'noteford:library-switch') { current = 'B'; throw new Error('interrupted'); }
    return input.library_id;
  });
  assert.equal(await api.status(), 'A');
  await assert.rejects(api.switchLibrary({library_id:'B'}), /interrupted/);
  assert.equal(await api.status(), 'B');
});

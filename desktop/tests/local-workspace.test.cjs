'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {LocalWorkspace}=require('../electron/local-workspace.cjs');
test('local lifecycle starts without cloud credentials and revokes stale calls after close',t=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'community-workspace-'));
 t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const workspace=new LocalWorkspace(root),scope=workspace.activate();
 assert.equal(scope.root,fs.realpathSync(root));
 assert.equal(workspace.run(scope,()=>workspace.assert().epoch),scope.epoch);
 assert.ok(fs.existsSync(path.join(root,'config.local.json')));
 assert.equal(fs.existsSync(path.join(root,'product-accounts')),false);
 workspace.lock();assert.throws(()=>workspace.run(scope,()=>true));
 const next=workspace.activate();assert.notEqual(next.epoch,scope.epoch);
 assert.throws(()=>workspace.assert(scope));workspace.lock();
});
test('community refuses a member-version data root before writing its lease',t=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'member-workspace-'));
 t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 fs.writeFileSync(path.join(root,'.product-workspace.json'),'{}');
 assert.throws(()=>new LocalWorkspace(root),/Refusing member/);
 assert.equal(fs.existsSync(path.join(root,'data')),false);
});

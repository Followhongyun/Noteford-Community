const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawnSync}=require('node:child_process');
const guard=path.join(__dirname,'check-private-data.cjs');
function fixture(t){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'source-layout-'));
 t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const git=(...args)=>{const r=spawnSync('git',args,{cwd:root,encoding:'utf8'});assert.equal(r.status,0,r.stderr);return r.stdout.trim();};
 const check=(args=[],input)=>spawnSync(process.execPath,[guard,...args],{cwd:root,input,encoding:'utf8'});
 git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid');
 fs.writeFileSync(path.join(root,'README.md'),'Synthetic fixture');git('add','README.md');
 return {root,git,check};
}
test('source files pass, but unregistered root files are rejected',t=>{
 const {root,git,check}=fixture(t);assert.equal(check().status,0);
 fs.mkdirSync(path.join(root,'temporary-project'));assert.equal(check().status,1);
 fs.writeFileSync(path.join(root,'temporary-project','test.txt'),'fixture');git('add','temporary-project');assert.equal(check().status,1);
});
test('private Data may exist locally but must never enter the index',t=>{
 const {root,git,check}=fixture(t);fs.mkdirSync(path.join(root,'Data'));fs.writeFileSync(path.join(root,'Data','private.txt'),'fixture');
 assert.equal(check().status,0);git('add','-f','Data');assert.equal(check().status,1);
});
test('push rejects private data in an intermediate commit after it was untracked',t=>{
 const {root,git,check}=fixture(t);git('commit','-qm','base');const base=git('rev-parse','HEAD');
 fs.mkdirSync(path.join(root,'Data'));fs.writeFileSync(path.join(root,'Data','private.txt'),'fixture');git('add','Data');git('commit','-qm','private fixture');
 git('rm','--cached','Data/private.txt');git('commit','-qm','untrack fixture');const head=git('rev-parse','HEAD');
 assert.equal(check().status,0);assert.equal(check(['--push'],`refs/heads/main ${head} refs/heads/main ${base}\n`).status,1);
});

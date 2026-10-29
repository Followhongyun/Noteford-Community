const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawnSync}=require('node:child_process');
const {check}=require('../desktop/scripts/private-data-policy.cjs');
const seam=require('../desktop/electron/platform-seam.cjs');
test('Mac source runtime stores data only in the checkout Data child',()=>{
 assert.equal(seam.runtimeRoot({platform:'darwin',packaged:false,portableLayout:false,projectRoot:'/tmp/source',home:'/tmp/home'}),'/tmp/source/Data');
});
test('packaging rejects aliases into Data and Git rejects forced private staging',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'noteford-layout-'));
 try{
  fs.mkdirSync(path.join(root,'Data'));fs.mkdirSync(path.join(root,'desktop/assets'),{recursive:true});
  fs.writeFileSync(path.join(root,'Data/private.txt'),'synthetic fixture');
  assert.doesNotThrow(()=>check(root));fs.symlinkSync(path.join(root,'Data'),path.join(root,'desktop/assets/alias'));
  assert.throws(()=>check(root),/私有数据/);
  for(const args of [['init','-q'],['add','-f','Data']])assert.equal(spawnSync('git',args,{cwd:root}).status,0);
  assert.equal(spawnSync(process.execPath,[path.resolve(__dirname,'../Scripts/check-private-data.cjs')],{cwd:root}).status,1);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('new branch checks unpublished history, including a secret deleted before the tip',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'noteford-push-'));
 const git=(...args)=>{const r=spawnSync('git',args,{cwd:root,encoding:'utf8'});assert.equal(r.status,0,r.stderr);return r.stdout.trim()};
 const checkPush=()=>spawnSync(process.execPath,[path.resolve(__dirname,'../Scripts/check-private-data.cjs'),'--push','--remote','origin'],{cwd:root,encoding:'utf8',input:`refs/heads/topic ${git('rev-parse','HEAD')} refs/heads/topic ${'0'.repeat(40)}\n`});
 try{
  git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid');git('remote','add','origin','https://example.invalid/repo');
  fs.mkdirSync(path.join(root,'build'));fs.writeFileSync(path.join(root,'build/old.txt'),'synthetic old artifact');git('add','.');git('commit','-qm','old published artifact');
  fs.unlinkSync(path.join(root,'build/old.txt'));git('add','-u');git('commit','-qm','remove old artifact');git('update-ref','refs/remotes/origin/main',git('rev-parse','HEAD'));
  fs.writeFileSync(path.join(root,'README.md'),'source');git('add','.');git('commit','-qm','source change');assert.equal(checkPush().status,0);
  fs.mkdirSync(path.join(root,'Data'));fs.writeFileSync(path.join(root,'Data/private.txt'),'synthetic secret');git('add','.');git('commit','-qm','accidental private file');
  fs.unlinkSync(path.join(root,'Data/private.txt'));git('add','-u');git('commit','-qm','delete private file');assert.equal(checkPush().status,1);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});

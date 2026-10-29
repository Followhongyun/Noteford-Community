const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const http=require('node:http');
const {createHash,randomBytes}=require('node:crypto');
const {CancellationToken,CancellationError}=require('builder-util-runtime');
const {resumableDownload}=require('../electron/resumable-download.cjs');
async function fixture(t,behavior=()=>{}) {
 const body=randomBytes(35*1024+7),sha512=createHash('sha512').update(body).digest('base64');
 const etag='"sha512-'+Buffer.from(sha512,'base64').toString('hex')+'"',calls=[];
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'noteford-resume-'));
 t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const server=http.createServer((req,res)=>{
  const match=/^bytes=(\d+)-(\d+)$/.exec(req.headers.range||'');assert.ok(match);
  const [start,end]=match.slice(1).map(Number);calls.push(start);
  assert.equal(req.headers.authorization,'Bearer synthetic');assert.equal(req.headers['if-range'],etag);
  if(behavior({req,res,start,end,calls,body,etag}))return;
  res.writeHead(206,{'content-length':end-start+1,'content-range':`bytes ${start}-${end}/${body.length}`,etag});res.end(body.subarray(start,end+1));
 });
 await new Promise(ok=>server.listen(0,'127.0.0.1',ok));t.after(()=>new Promise(ok=>server.close(ok)));
 const destination=path.join(root,'pending','complete.zip'),resumeRoot=path.join(root,'noteford-resume');
 await fs.mkdir(path.dirname(destination));
 const url=new URL(`http://127.0.0.1:${server.address().port}/Noteford.zip`);
 const executor={createRequest:(options,callback)=>http.request(options,callback)};
 const run=(more={})=>resumableDownload(executor,url,destination,{sha512,headers:{authorization:'Bearer synthetic'},cancellationToken:new CancellationToken(),...more},body.length,{chunkSize:8192,retryDelay:1,attempts:3});
 return {body,calls,run,root,resumeRoot,destination};
}
test('truncated response and temporary upstream error retry only the unfinished chunk; complete checksum matches',async t=>{
 let interrupted=false,failed=false;
 const f=await fixture(t,({res,start,end,body,etag})=>{
  if(start===0&&!interrupted){interrupted=true;res.writeHead(206,{'content-length':end-start+1,'content-range':`bytes ${start}-${end}/${body.length}`,etag});res.write(body.subarray(0,2000));setImmediate(()=>res.destroy());return true;}
  if(start===8192&&!failed){failed=true;res.writeHead(503);res.end();return true;}
 });
 await f.run();assert.deepEqual(await fs.readFile(f.destination),f.body);assert.deepEqual(f.calls,[0,0,8192,8192,16384,24576,32768]);
 assert.deepEqual(await fs.readdir(f.resumeRoot),[]);
});
test('authorization failure does not retry, and a subsequent session resumes verified chunks without persisting credentials',async t=>{
 let denied=true;
 const f=await fixture(t,({res,start})=>{if(start===8192&&denied){res.writeHead(403);res.end();return true;}});
 await assert.rejects(f.run(),e=>e.statusCode===403);assert.deepEqual(f.calls,[0,8192]);
 const names=await fs.readdir(f.resumeRoot);const json=await fs.readFile(path.join(f.resumeRoot,names.find(n=>n.endsWith('.json'))),'utf8');assert.ok(!json.includes('synthetic'));
 await fs.rm(path.dirname(f.destination),{recursive:true});await fs.mkdir(path.dirname(f.destination));
 denied=false;await f.run();assert.deepEqual(f.calls,[0,8192,8192,16384,24576,32768]);assert.deepEqual(await fs.readFile(f.destination),f.body);
});
test('corrupt saved prefix is discarded before resuming',async t=>{
 let denied=true;
 const f=await fixture(t,({res,start})=>{if(start===8192&&denied){res.writeHead(401);res.end();return true;}});
 await assert.rejects(f.run());const names=await fs.readdir(f.resumeRoot);
 await fs.writeFile(path.join(f.resumeRoot,names.find(n=>n.endsWith('.partial'))),Buffer.alloc(8192));
 denied=false;await f.run();assert.equal(f.calls[2],0);assert.deepEqual(await fs.readFile(f.destination),f.body);
});
test('ignored ranges, redirected requests, changed validators and incorrect ranges fail closed',async t=>{
 for(const mode of ['ignored','redirect','etag','range']) {
  const f=await fixture(t,({res,start,end,body,etag})=>{
   res.writeHead(mode==='ignored'?200:mode==='redirect'?302:206,{'content-length':end-start+1,'content-range':mode==='range'?'bytes 1-2/3':`bytes ${start}-${end}/${body.length}`,etag:mode==='etag'?'"different"':etag,location:'https://outside.test/'});res.end(body.subarray(start,end+1));return true;
  });
  await assert.rejects(f.run());assert.equal(f.calls.length,1);await assert.rejects(fs.stat(f.destination),{code:'ENOENT'});
 }
});
test('a complete but altered file is rejected by final SHA-512 and never delivered',async t=>{
 const f=await fixture(t,({res,start,end,body,etag})=>{
  const bytes=Buffer.from(body.subarray(start,end+1));bytes[0]^=1;
  res.writeHead(206,{'content-length':bytes.length,'content-range':`bytes ${start}-${end}/${body.length}`,etag});res.end(bytes);return true;
 });
 await assert.rejects(f.run(),{code:'ERR_CHECKSUM_MISMATCH'});await assert.rejects(fs.stat(f.destination),{code:'ENOENT'});
});
test('cancellation closes the in-flight request and preserves no incomplete chunk',async t=>{
 const token=new CancellationToken();
 const f=await fixture(t,({res,start,end,body,etag})=>{
  res.writeHead(206,{'content-length':end-start+1,'content-range':`bytes ${start}-${end}/${body.length}`,etag});res.write(body.subarray(start,start+128));setTimeout(()=>token.cancel(),10);return true;
 });
 await assert.rejects(f.run({cancellationToken:token}),e=>e instanceof CancellationError);assert.equal(f.calls.length,1);
 await assert.rejects(fs.stat(f.destination),{code:'ENOENT'});
});

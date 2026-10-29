const {test}=require('node:test');
const assert=require('node:assert/strict');
const http=require('node:http');
const {downloadRequest}=require('../electron/long-download-request.cjs');
test('long local download waits for result and workspace revocation aborts the wait',async()=>{
  const server=http.createServer((req,res)=>{req.resume();if(req.url==='/wait')return;setTimeout(()=>{res.end(JSON.stringify({ok:true,data:{status:'ready'}}));},40);});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  try {
    const base=`http://127.0.0.1:${server.address().port}`;
    const response=await downloadRequest(base+'/result',{headers:{'Content-Type':'application/json'},body:'{}'});
    assert.equal(response.ok,true);assert.equal((await response.json()).data.status,'ready');
    const abort=new AbortController();
    const waiting=downloadRequest(base+'/wait',{signal:abort.signal,body:'{}'});
    abort.abort();await assert.rejects(waiting,{name:'AbortError'});
    assert.throws(()=>downloadRequest('https://example.com/',{}),/invalid_service_origin/);
  } finally {server.closeAllConnections();await new Promise(r=>server.close(r));}
});

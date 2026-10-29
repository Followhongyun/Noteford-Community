'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { CancellationError } = require('builder-util-runtime');
const CHUNK_SIZE = 2 * 1024 * 1024;
function error(code, statusCode) { return Object.assign(new Error(code), { code, statusCode }); }
function cancelled(token) { if (token?.cancelled) throw new CancellationError(); }
function digest(data) { return createHash('sha256').update(data).digest('hex'); }
function header(response, name) {
  const value = response.headers[name]; return Array.isArray(value) ? value[0] : value;
}

// Each response is small and independently retried. Credentials are never stored.
function readRange(executor, url, options, start, end, size, etag) {
  return options.cancellationToken.createPromise((resolve, reject, onCancel) => {
    let request, response, settled = false, timer;
    const finish = (failure, value) => {
      if (settled) return;
      settled = true; clearTimeout(timer);
      if (failure) { request?.abort?.(); response?.destroy?.(); reject(failure); }
      else resolve(value);
    };
    const idle = () => { clearTimeout(timer); timer = setTimeout(() => finish(error('ETIMEDOUT')), 60000); timer.unref?.(); };
    try {
      request = executor.createRequest({ protocol:url.protocol, hostname:url.hostname, port:url.port || undefined,
        path:url.pathname, method:'GET', redirect:'manual', headers:{ ...options.headers,
          Range:`bytes=${start}-${end}`, 'If-Range':etag, 'Accept-Encoding':'identity' } }, res => {
        response = res; idle();
        if (res.statusCode !== 206) return finish(error('ERR_UPDATE_RANGE_STATUS', res.statusCode));
        if (header(res,'content-range') !== `bytes ${start}-${end}/${size}`
            || Number(header(res,'content-length')) !== end-start+1
            || header(res,'etag') !== etag || header(res,'content-encoding'))
          return finish(error('ERR_UPDATE_RANGE_HEADERS'));
        const chunks=[];let received=0;
        res.on('data', chunk => {
          idle(); received+=chunk.length;
          if (received>end-start+1) return finish(error('ERR_UPDATE_RANGE_LENGTH'));
          chunks.push(chunk);
        });
        res.on('end',()=>finish(received===end-start+1 ? null : error('ECONNRESET'), Buffer.concat(chunks,received)));
        res.on('aborted',()=>finish(error('ECONNRESET')));
        res.on('error',()=>finish(error('ECONNRESET')));
        res.on('close',()=>{if(!settled)finish(error('ECONNRESET'));});
      });
      onCancel(()=>finish(new CancellationError()));
      request.on('error',()=>finish(error('ECONNRESET')));
      request.on('redirect',()=>finish(error('ERR_UPDATE_REDIRECT')));
      idle();request.end();
    } catch (failure) { finish(failure); }
  });
}
function delay(ms, token) {
  return token.createPromise((resolve,reject,onCancel)=>{
    const timer=setTimeout(resolve,ms);onCancel(()=>{clearTimeout(timer);reject(new CancellationError());});
  });
}
async function resumableDownload(executor, url, destination, options, size, settings={}) {
  const chunkSize=settings.chunkSize || CHUNK_SIZE;
  const attempts=settings.attempts || 5;
  if (!Number.isSafeInteger(size) || size<1 || size>2*1024**3 || !/^[A-Za-z0-9+/]{86}==$/.test(options.sha512 || ''))
    throw error('ERR_UPDATE_METADATA');
  const expected=Buffer.from(options.sha512,'base64').toString('hex');
  const etag=`"sha512-${expected}"`;
  // Outside electron-updater's pending directory, which it empties after failure.
  const root=settings.resumeRoot || path.join(path.dirname(path.dirname(destination)),'noteford-resume');
  await fs.promises.mkdir(root,{recursive:true,mode:0o700});
  const partial=path.join(root,expected+'.partial'), metadata=partial+'.json';
  const identity={url:url.href,size,sha512:options.sha512,chunkSize};
  let state={...identity,chunks:[]};
  try {
    const previous=JSON.parse(await fs.promises.readFile(metadata,'utf8'));
    if (Object.keys(identity).every(k=>previous[k]===identity[k]) && Array.isArray(previous.chunks)
        && previous.chunks.length<=Math.ceil(size/chunkSize)) state=previous;
  } catch {}
  const file=await fs.promises.open(partial,fs.existsSync(partial)?'r+':'w+',0o600);
  let offset=0;
  const hash=createHash('sha512');
  const started=Date.now();let initial=0,reported=0;
  const progress=()=>{options.onProgress?.({total:size,transferred:offset,percent:100*offset/size,
    delta:offset-reported,bytesPerSecond:(offset-initial)/Math.max(1,(Date.now()-started)/1000)});reported=offset;};
  try {
    // Re-check persisted chunks; a corrupt or incomplete tail is fetched again.
    let valid=0;
    for(const sum of state.chunks) {
      cancelled(options.cancellationToken);
      const length=Math.min(chunkSize,size-offset),buffer=Buffer.alloc(length);
      const {bytesRead}=await file.read(buffer,0,length,offset);
      if(bytesRead!==length || digest(buffer)!==sum)break;
      hash.update(buffer);offset+=length;valid++;
    }
    state.chunks=state.chunks.slice(0,valid);await file.truncate(offset);initial=offset;progress();
    while(offset<size) {
      cancelled(options.cancellationToken);
      const end=Math.min(size-1,offset+chunkSize-1);let buffer;
      for(let attempt=0;attempt<attempts;attempt++) {
        try { buffer=await readRange(executor,url,options,offset,end,size,etag);break; }
        catch(failure) {
          cancelled(options.cancellationToken);
          const retry=['ECONNRESET','ETIMEDOUT'].includes(failure.code) || [408,429,500,502,503,504].includes(failure.statusCode);
          if(!retry || attempt===attempts-1)throw failure;
          await delay(settings.retryDelay ?? Math.min(1000*2**attempt,8000),options.cancellationToken);
        }
      }
      let written=0;
      while(written<buffer.length) { const r=await file.write(buffer,written,buffer.length-written,offset+written);if(!r.bytesWritten)throw error('ERR_UPDATE_DISK_WRITE');written+=r.bytesWritten; }
      await file.sync();hash.update(buffer);offset+=buffer.length;state.chunks.push(digest(buffer));
      const temp=metadata+'.new';await fs.promises.writeFile(temp,JSON.stringify(state),{mode:0o600});await fs.promises.rename(temp,metadata);progress();
    }
    if(hash.digest('hex')!==expected) {
      await file.truncate(0);await fs.promises.rm(metadata,{force:true});throw error('ERR_CHECKSUM_MISMATCH');
    }
  } finally { await file.close(); }
  cancelled(options.cancellationToken);
  await fs.promises.rename(partial,destination);await fs.promises.rm(metadata,{force:true});
  return destination;
}
module.exports={resumableDownload};

'use strict';
// The local service may take hours to fetch a large original. Node's HTTP client
// avoids fetch's fixed header deadline, while workspace revocation still aborts it.
const http = require('node:http');
function downloadRequest(url, {headers, body, signal}) {
  const target = new URL(url);
  if (target.protocol !== 'http:' || target.hostname !== '127.0.0.1') throw Error('invalid_service_origin');
  return new Promise((resolve, reject) => {
    const request = http.request(target, {method:'POST', headers:{...headers, 'Content-Length':Buffer.byteLength(body || '')}, signal}, response => {
      const parts = []; let size = 0;
      response.on('data', part => {
        size += part.length;
        // Only the small JSON result crosses IPC; the file stays on disk.
        if (size > 16 * 1024 * 1024) {request.destroy(Error('invalid_service_response')); return;}
        parts.push(part);
      });
      response.on('error', reject);
      response.on('end', () => {
        try {resolve({ok:response.statusCode >= 200 && response.statusCode < 300,
          json:async () => JSON.parse(Buffer.concat(parts).toString('utf8'))});}
        catch {reject(Error('invalid_service_response'));}
      });
    });
    request.on('error', reject);
    request.end(body);
  });
}
module.exports = {downloadRequest};

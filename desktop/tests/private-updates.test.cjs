const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { createHash } = require('node:crypto');
const { createPrivateUpdates, desktopAuthenticator } = require('../../distribution/private-updates.cjs');
test('download page, manifest and direct files all require current account access; no path or symlink bypass', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'noteford-feed-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.writeFile(path.join(root, 'latest-mac.yml'), 'version: 0.1.0');
  await fs.writeFile(path.join(root, 'Noteford.dmg'), 'signed fixture');
  await fs.symlink('/etc/hosts', path.join(root, 'escape.zip'));
  let entitled = true;
  const server = http.createServer(createPrivateUpdates({ root,
    release: { version: '0.1.0', files: ['Noteford.dmg', 'escape.zip'] },
    authenticate: async req => ({ id: req.headers.authorization === 'Bearer fixture' ? 'test-user' : null, notefordAccess: entitled }),
  }));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const url = `http://127.0.0.1:${server.address().port}`;
  const auth = { headers: { authorization: 'Bearer fixture' } };
  for (const route of ['/downloads/noteford', '/updates/noteford/macos/latest-mac.yml', '/updates/noteford/macos/Noteford.dmg']) {
    assert.equal((await fetch(url + route)).status, 403);
    const response = await fetch(url + route, auth);
    assert.equal(response.status, 200);
    if (route.endsWith('.dmg')) assert.equal(response.headers.get('content-type'), 'application/x-apple-diskimage');
    assert.equal(response.headers.get('cache-control'), 'private, no-store, no-transform');
  }
  assert.equal((await fetch(url + '/updates/noteford/macos/escape.zip', auth)).status, 404);
  assert.equal((await fetch(url + '/updates/noteford/macos/%2e%2e/package.json', auth)).status, 404);
  entitled = false;
  assert.equal((await fetch(url + '/updates/noteford/macos/Noteford.dmg', auth)).status, 403);
});
test('auth validates desktop session server-side on every request, fails closed without browser SSO', async () => {
  let valid = true, count = 0;
  const authenticate = desktopAuthenticator({ apiOrigin: 'https://api.muxuno.com',
    fetcher: async (url, options) => {
      count++; assert.equal(options.redirect, 'error');
      return Response.json(valid ? { version: 'noteford-identity-v1', product: 'noteford', account: { id: 'test' } } : {}, { status: valid ? 200 : 401 });
    } });
  assert.equal(await authenticate({ headers: {} }), null);
  const req = { headers: { authorization: 'Bearer nfs_' + 's'.repeat(43) } };
  assert.equal((await authenticate(req)).notefordAccess, true);
  valid = false; await assert.rejects(authenticate(req)); assert.equal(count, 2);
});

test('byte ranges preserve exact bytes, validators and per-request access without shared caching', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'noteford-range-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const body = Buffer.from('0123456789abcdef'), name = 'Noteford.zip';
  await fs.writeFile(path.join(root, name), body);
  const sha512 = createHash('sha512').update(body).digest('hex');
  let entitled = true, authCalls = 0;
  const server = http.createServer(createPrivateUpdates({ root,
    release: { version:'0.1.3', files:[name], artifacts:[{name,size:body.length,sha512}] },
    authenticate: async req => {authCalls++; return { id:req.headers.authorization==='Bearer fixture' ? 'test' : null, notefordAccess:entitled };},
  }));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const url = `http://127.0.0.1:${server.address().port}/updates/noteford/macos/${name}`;
  const get = (range, more={}, method='GET') => fetch(url,{method,headers:{authorization:'Bearer fixture',...(range?{range}:{}),...more}});
  const full = await get();
  assert.equal(full.headers.get('content-type'),'application/zip');
  assert.equal(full.status,200); assert.deepEqual(Buffer.from(await full.arrayBuffer()),body);
  const etag=full.headers.get('etag'); assert.equal(etag,`"sha512-${sha512}"`);
  for (const [range,start,end] of [['bytes=0-3',0,3],['bytes=4-',4,15],['bytes=-4',12,15],['bytes=12-9999999999999999999999',12,15],['bytes=-9999999999999999999999',0,15]]) {
    const response=await get(range,{'if-range':etag});
    assert.equal(response.status,206);assert.equal(response.headers.get('content-range'),`bytes ${start}-${end}/16`);
    assert.equal(response.headers.get('content-length'),String(end-start+1));
    assert.equal(response.headers.get('cache-control'),'private, no-store, no-transform');
    assert.equal(response.headers.get('accept-ranges'),'bytes');
    assert.deepEqual(Buffer.from(await response.arrayBuffer()),body.subarray(start,end+1));
  }
  for (const range of ['bytes=16-','bytes=-0','bytes=9999999999999999999999-']) {
    const response=await get(range);assert.equal(response.status,416);
    assert.equal(response.headers.get('content-range'),'bytes */16');assert.equal(await response.text(),'');
    assert.equal(response.headers.get('cache-control'),'private, no-store, no-transform');
  }
  for (const range of ['bytes=9-2','bytes=-','other=0-1','bytes=0-15,0-15','bytes='+'9'.repeat(161)+'-']) {
    const response=await get(range);assert.equal(response.status,200);
    assert.deepEqual(Buffer.from(await response.arrayBuffer()),body);
  }
  for (const validator of ['"old"','W/'+etag,'Wed, 21 Oct 2015 07:28:00 GMT']) {
    const response=await get('bytes=0-3',{'if-range':validator});assert.equal(response.status,200);
    assert.deepEqual(Buffer.from(await response.arrayBuffer()),body);
  }
  const multi=await get('bytes=0-1, 4-5',{'if-range':etag});
  assert.equal(multi.status,206);
  const boundary=multi.headers.get('content-type').split('boundary=')[1];
  const multiBody=await multi.text();
  assert.equal(Number(multi.headers.get('content-length')),Buffer.byteLength(multiBody));
  assert.equal(multiBody,`--${boundary}\r\nContent-Type: application/zip\r\nContent-Range: bytes 0-1/16\r\n\r\n01\r\n--${boundary}\r\nContent-Type: application/zip\r\nContent-Range: bytes 4-5/16\r\n\r\n45\r\n--${boundary}--\r\n`);
  const head=await get('bytes=0-3',{},'HEAD');assert.equal(head.status,200);
  assert.equal(head.headers.get('content-length'),'16');assert.equal(head.headers.get('content-range'),null);assert.equal(await head.text(),'');
  const anonymous=await fetch(url,{headers:{range:'bytes=999-'}});assert.equal(anonymous.status,403);
  assert.equal(anonymous.headers.get('content-range'),null);assert.equal(anonymous.headers.get('etag'),null);
  entitled=false;const revoked=await get('bytes=0-3');assert.equal(revoked.status,403);
  assert.equal(revoked.headers.get('content-range'),null);assert.equal(revoked.headers.get('etag'),null);
  assert.equal(authCalls,21);
});

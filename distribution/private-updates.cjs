'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { pipeline } = require('node:stream/promises');

// One bounded byte range. Unsupported/malformed ranges fall
// back to the existing full response; valid but unsatisfiable ranges use 416.
function multipleRanges(value, size) {
  if (typeof value !== 'string' || value.length > 16000 || !value.startsWith('bytes=') || !value.includes(',')) return null;
  const parts = value.slice(6).split(',');
  if (parts.length > 1000) return null;
  const ranges = parts.map(part => byteRange('bytes=' + part.trim(), size));
  if (ranges.some(range => !range || range.unsatisfiable)) return null;
  // Bound amplification and preserve the order the native updater requested.
  if (ranges.reduce((sum, range) => sum + range.end - range.start + 1, 0) > size) return null;
  return ranges;
}
function byteRange(value, size) {
  if (typeof value !== 'string' || value.length > 160 || size === 0) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2])) return null;
  const length = BigInt(size);
  if (!match[1]) {
    const suffix = BigInt(match[2]);
    if (suffix === 0n) return { unsatisfiable: true };
    return { start: Number(suffix >= length ? 0n : length - suffix), end: size - 1 };
  }
  const start = BigInt(match[1]), end = match[2] ? BigInt(match[2]) : length - 1n;
  if (match[2] && end < start) return null;
  if (start >= length) return { unsatisfiable: true };
  return { start: Number(start), end: Number(end >= length ? length - 1n : end) };
}

// Mount behind HTTPS on followhongyun.com. Both browser sessions and desktop
// sessions must resolve to a currently entitled Noteford account. No public
// object-storage URLs or static-directory fallback may bypass this handler.
function createPrivateUpdates({ root, authenticate, release }) {
  const base = '/updates/noteford/macos/';
  const headers = { 'cache-control': 'private, no-store, no-transform', 'x-robots-tag': 'noindex, nofollow, noarchive',
    'x-content-type-options': 'nosniff', 'referrer-policy': 'no-referrer' };
  return async (req, res) => {
    const send = (code, message) => { res.writeHead(code, headers); res.end(message); };
    if (!['GET', 'HEAD'].includes(req.method)) return send(405, 'Method not allowed');
    let url;
    try { url = new URL(req.url, 'https://followhongyun.com'); }
    catch { return send(400, 'Invalid request'); }
    if (!(url.pathname.startsWith(base) || url.pathname === '/downloads/noteford')) return send(404, 'Not found');
    try {
      const account = await authenticate(req);
      if (!account?.id || account.notefordAccess !== true) return send(403, '请先登录 Followhongyun 账号。');
    } catch { return send(401, '请先登录 Followhongyun 账号。'); }
    if (!release) return send(503, '版本尚未发布。');
    // Explicit allowlist from release metadata; never serve arbitrary paths.
    const allowed = new Set(['latest-mac.yml', ...release.files]);
    if ([...allowed].some(name => !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name))) return send(503, 'Invalid release');
    if (url.pathname === '/downloads/noteford') {
      const dmg = release.files.find(name => name.endsWith('.dmg'));
      if (!dmg || !/^\d+\.\d+\.\d+$/.test(release.version)) return send(503, '版本尚未发布。');
      res.writeHead(200, { ...headers, 'content-type': 'text/html; charset=utf-8',
        'content-security-policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'" });
      return res.end(req.method === 'HEAD' ? '' : `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>下载 Noteford</title><style>body{font:17px system-ui;background:#f5f7f3;color:#25382a;max-width:520px;margin:16vh auto;padding:32px}a{display:inline-block;color:white;background:#4c7053;padding:14px 24px;border-radius:12px;text-decoration:none}</style><h1>Noteford</h1><p>从 Noteford 开始，Follow洪云，让默契自然发生。</p><p>macOS · ${release.version}</p><a href="${base}${dmg}">下载 Mac 版</a></html>`);
    }
    const name = url.pathname.slice(base.length);
    if (!allowed.has(name)) return send(404, 'Not found');
    try {
      const file = path.resolve(root, name), resolved = await fs.promises.realpath(file);
      if (path.dirname(resolved) !== await fs.promises.realpath(root)) return send(404, 'Not found');
      const stat = await fs.promises.stat(resolved);
      if (!stat.isFile()) return send(404, 'Not found');
      // The origin provides checksums only after verifying immutable artifacts
      // at startup. No request-time full-file hashing or credential caching.
      const checksum = release.artifacts?.find(a => a.name === name && a.size === stat.size)?.sha512;
      const etag = typeof checksum === 'string' && /^[a-f0-9]{128}$/.test(checksum)
        ? `"sha512-${checksum}"` : null;
      const fileHeaders = { ...headers, 'accept-ranges': 'bytes',
        'content-type': name.endsWith('.yml') ? 'text/yaml; charset=utf-8'
          : name.endsWith('.zip') ? 'application/zip'
          : name.endsWith('.dmg') ? 'application/x-apple-diskimage'
          : name.endsWith('.blockmap') ? 'application/gzip' : 'application/octet-stream',
        'content-disposition': `attachment; filename="${name}"`, ...(etag ? { etag } : {}) };
      // HEAD ignores Range. An unavailable, weak, date or changed If-Range
      // validator safely falls back to the whole current artifact.
      const multipart = req.method === 'GET' && (!req.headers['if-range'] || (etag && req.headers['if-range'] === etag))
        ? multipleRanges(req.headers.range, stat.size) : null;
      if (multipart) {
        const boundary = 'noteford-' + require('node:crypto').randomBytes(16).toString('hex');
        const prefixes = multipart.map(range => Buffer.from(`--${boundary}\r\nContent-Type: ${fileHeaders['content-type']}\r\nContent-Range: bytes ${range.start}-${range.end}/${stat.size}\r\n\r\n`));
        const tail = Buffer.from(`--${boundary}--\r\n`);
        const length = multipart.reduce((sum, range, i) => sum + prefixes[i].length + range.end - range.start + 1 + 2, tail.length);
        res.writeHead(206, { ...fileHeaders, 'content-type': `multipart/byteranges; boundary=${boundary}`, 'content-length': length });
        async function* chunks() {
          for (let i = 0; i < multipart.length; i++) {
            yield prefixes[i];
            yield* fs.createReadStream(resolved, multipart[i]);
            yield Buffer.from('\r\n');
          }
          yield tail;
        }
        await pipeline(require('node:stream').Readable.from(chunks()), res);
        return;
      }
      const range = req.method === 'GET' && (!req.headers['if-range'] || (etag && req.headers['if-range'] === etag))
        ? byteRange(req.headers.range, stat.size) : null;
      if (range?.unsatisfiable) {
        res.writeHead(416, { ...fileHeaders, 'content-range': `bytes */${stat.size}`, 'content-length': 0 });
        return res.end();
      }
      res.writeHead(range ? 206 : 200, { ...fileHeaders,
        'content-length': range ? range.end - range.start + 1 : stat.size,
        ...(range ? { 'content-range': `bytes ${range.start}-${range.end}/${stat.size}` } : {}) });
      if (req.method === 'HEAD') return res.end();
      await pipeline(fs.createReadStream(resolved, range || undefined), res);
    } catch { if (!res.headersSent) send(404, 'Not found'); else res.destroy(); }
  };
}

function desktopAuthenticator({ apiOrigin, fetcher = fetch, browserAccount = async () => null }) {
  if (!/^https:\/\/[^/]+$/.test(apiOrigin)) throw Error('HTTPS account origin required');
  return async req => {
    const token = req.headers.authorization;
    if (!token) return browserAccount(req); // Existing verified, server-side SSO session; deny by default.
    if (!/^Bearer nfs_[A-Za-z0-9_-]{43}$/.test(token)) throw Error('Unauthenticated');
    const response = await fetcher(apiOrigin + '/v2/products/noteford/me', {
      headers: { authorization: token }, redirect: 'error', signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw Error('Unauthenticated');
    const body = await response.json();
    if (body.version !== 'noteford-identity-v1' || body.product !== 'noteford' || !body.account?.id)
      throw Error('Unauthenticated');
    return { id: body.account.id, notefordAccess: true };
  };
}
module.exports = { createPrivateUpdates, desktopAuthenticator };

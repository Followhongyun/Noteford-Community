'use strict';
// A dedicated, read-only update origin. The gateway exposes only the fixed Mac
// update prefix; this process has no access to account databases or user files.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { createHash } = require('node:crypto');
const { createPrivateUpdates, desktopAuthenticator } = require('./private-updates.cjs');
const API_ORIGIN = 'https://api.muxuno.com';

async function verifyRelease(root) {
  const realRoot = await fs.promises.realpath(root);
  const release = JSON.parse(await fs.promises.readFile(path.join(realRoot, 'release.json'), 'utf8'));
  if (!/^\d+\.\d+\.\d+$/.test(release.version) || !Array.isArray(release.artifacts)
      || !release.artifacts.some(a => a.name === 'latest-mac.yml')
      || !release.artifacts.some(a => a.name.endsWith('-arm64.zip')))
    throw Error('Invalid release manifest');
  // Optional immutable sidecars add differential downloads without rewriting
  // the signed artifacts or original release manifest.
  let supplemental;
  try { supplemental = JSON.parse(await fs.promises.readFile(path.join(realRoot, 'blockmaps.json'), 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (supplemental) {
    if (supplemental.version !== release.version || !Array.isArray(supplemental.artifacts)
        || supplemental.artifacts.length > 4 || supplemental.artifacts.some(a =>
          !/^Noteford-\d+\.\d+\.\d+-arm64\.zip\.blockmap$/.test(a.name)
          || !Number.isSafeInteger(a.size) || a.size < 1 || a.size > 1024 * 1024))
      throw Error('Invalid blockmap manifest');
    release.artifacts = [...release.artifacts, ...supplemental.artifacts];
  }
  const names = new Set();
  for (const artifact of release.artifacts) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(artifact.name) || names.has(artifact.name)
        || !Number.isSafeInteger(artifact.size) || artifact.size < 1
        || !/^[a-f0-9]{128}$/.test(artifact.sha512)) throw Error('Invalid artifact');
    names.add(artifact.name);
    const file = await fs.promises.realpath(path.join(realRoot, artifact.name));
    const stat = await fs.promises.stat(file);
    if (path.dirname(file) !== realRoot || !stat.isFile() || stat.size !== artifact.size)
      throw Error('Artifact size or path mismatch');
    const hash = createHash('sha512');
    for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
    if (hash.digest('hex') !== artifact.sha512) throw Error('Artifact checksum mismatch');
  }
  return { version: release.version, files: [...names],
    artifacts: release.artifacts.map(({ name, size, sha512 }) => ({ name, size, sha512 })) };
}

function gatedAuthenticator({ accessFile, authenticate = desktopAuthenticator({ apiOrigin: API_ORIGIN }) }) {
  return async req => {
    const account = await authenticate(req);
    if (!account?.id || account.notefordAccess !== true) return null;
    // Read on every request: publication/withdrawal is an atomic file rename,
    // and never grants access to an account rejected by the identity service.
    const access = JSON.parse(await fs.promises.readFile(accessFile, 'utf8'));
    if (access.mode === 'entitled') return account;
    if (access.mode !== 'canary' || !Array.isArray(access.accountHashes)) return null;
    const hash = createHash('sha256').update(API_ORIGIN + '\n' + account.id.toLowerCase()).digest('hex');
    return access.accountHashes.includes(hash) ? account : null;
  };
}

async function start({ root, accessFile, port = 8080, host = '0.0.0.0' }) {
  const release = await verifyRelease(root);
  const handler = createPrivateUpdates({ root, release,
    authenticate: gatedAuthenticator({ accessFile }) });
  const server = http.createServer((req, res) => {
    if (req.url === '/healthz' && ['GET', 'HEAD'].includes(req.method)) {
      res.writeHead(200, { 'cache-control': 'no-store', 'content-type': 'text/plain' });
      return res.end(req.method === 'HEAD' ? '' : 'ready');
    }
    // No website/download page and no general-purpose static file server.
    if (!req.url.startsWith('/updates/noteford/macos/')) {
      res.writeHead(404, { 'cache-control': 'no-store' }); return res.end();
    }
    Promise.resolve(handler(req, res)).catch(() => {
      if (!res.headersSent) res.writeHead(503, { 'cache-control': 'no-store' });
      res.end();
    });
  });
  server.requestTimeout = 30_000;
  server.headersTimeout = 15_000;
  await new Promise((resolve, reject) => {
    server.once('error', reject); server.listen(port, host, resolve);
  });
  return server;
}
if (require.main === module) {
  // Shared pinned CA policy also protects this independent Node TLS path.
  require('../desktop/scripts/node-trust.cjs');
  start({ root: process.env.NOTEFORD_RELEASE_ROOT || '/release',
    accessFile: process.env.NOTEFORD_ACCESS_FILE || '/policy/access.json' })
    .then(server => {
      console.log('Noteford Mac update service ready');
      process.on('SIGTERM', () => server.close(() => process.exit(0)));
    }).catch(() => { console.error('Noteford update release validation failed'); process.exit(1); });
}
module.exports = { verifyRelease, gatedAuthenticator, start };

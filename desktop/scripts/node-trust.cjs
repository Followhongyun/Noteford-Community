'use strict';
// Standalone build Node is independent of Electron's certificate verifier.
const fs = require('node:fs');
const crypto = require('node:crypto');
const tls = require('node:tls');
const path = require('node:path');
const policy = require('../security/ca-policy.json');
const caFile = path.join(__dirname, '../security/cacert.pem');
const pem = fs.readFileSync(caFile, 'utf8');
if (crypto.createHash('sha256').update(pem).digest('hex') !== policy.pemSha256) {
  throw Error('Build CA bundle verification failed');
}
if (typeof tls.setDefaultCACertificates !== 'function') {
  throw Error('Build requires Node >=22.19 with tls.setDefaultCACertificates');
}
const roots = pem.match(/-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g);
if (!roots?.length) throw Error('Build CA bundle is empty');
tls.setDefaultCACertificates(roots);
process.env.npm_config_cafile = caFile;
// Scope the replacement to this build process tree, including npm lifecycle
// scripts, Electron downloads, node-gyp and workers. Do not mutate host trust.
const option = `--require=${JSON.stringify(__filename)}`;
if (!(process.env.NODE_OPTIONS || '').includes(option)) {
  process.env.NODE_OPTIONS = [process.env.NODE_OPTIONS, option].filter(Boolean).join(' ');
}

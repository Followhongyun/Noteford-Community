'use strict';
// A private local capability authenticates discovery/transport. Both Accounts
// identities are independently verified by the pinned Accounts service.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { randomBytes, timingSafeEqual } = require('node:crypto');
const { atomicJson } = require('./product-workspace.cjs');
const equal = (a, b) => typeof a === 'string' && a.length === b.length && timingSafeEqual(Buffer.from(a), Buffer.from(b));
class NexusEvents {
  constructor({ root, discoveryFile, identity, apiOrigin, request, fetcher = fetch, check = () => {} }) {
    Object.assign(this, { root, discoveryFile, identity, apiOrigin, request, fetcher, check });
    this.clients = new Map(); this.closed = false;
    this.capability = randomBytes(32).toString('base64url');
  }
  async authorize(token) {
    this.check();
    const own = await this.identity();
    if (!/^[A-Za-z0-9_-]{32,256}$/.test(token || '')) throw Error('unauthorized');
    const response = await this.fetcher(this.apiOrigin + '/v2/products/nexus/me', {
      headers: { authorization: 'Bearer ' + token }, redirect: 'error', signal: AbortSignal.timeout(12000),
    });
    const body = await response.text();
    if (body.length > 16384) throw Error('unauthorized');
    const peer = JSON.parse(body);
    if (!response.ok || peer.version !== 'nexus-identity-v1' || peer.product !== 'nexus'
        || peer.account?.id?.toLowerCase() !== own.id || !Number.isFinite(Date.parse(peer.expiresAt)) || Date.parse(peer.expiresAt) <= Date.now()) throw Error('unauthorized');
    this.check();
    if (this.closed) throw Error('closed');
    return own.id;
  }
  async start() {
    const directory = path.join(this.root, 'data/runtime/nexus-events');
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    this.watcher = fs.watch(directory, () => { for (const client of this.clients.values()) void this.pump(client); });
    this.watcher.on('error', () => this.close());
    this.server = http.createServer((req, res) => { void this.handle(req, res).catch(() => { if (!res.headersSent) res.writeHead(401); res.end(); }); });
    this.server.requestTimeout = 15000; this.server.headersTimeout = 15000;
    await new Promise((resolve, reject) => { this.server.once('error', reject); this.server.listen(0, '127.0.0.1', resolve); });
    if (this.closed) { this.server.close(); return; }
    atomicJson(this.discoveryFile, { version: 2, port: this.server.address().port, capability: this.capability });
    this.heartbeat = setInterval(() => {
      for (const client of this.clients.values()) void this.authorize(client.token).then(() => {
        if (!client.res.destroyed) client.res.write(': authenticated heartbeat\n\n');
      }).catch(() => client.res.destroy());
    }, 30000);
    this.heartbeat.unref();
  }
  async handle(req, res) {
    if (req.headers.origin || !equal(req.headers.authorization, 'Bearer ' + this.capability)) throw Error('unauthorized');
    if (req.method === 'GET' && req.url === '/events') {
      if (this.clients.size >= 4) { res.writeHead(429); res.end(); return; }
      const token = req.headers['x-accounts-session'];
      const owner = await this.authorize(token);
      const id = randomBytes(32).toString('base64url');
      const client = { id, token, owner, res, sent: new Set(), flight: null, dirty: false };
      this.clients.set(id, client);
      res.on('close', () => this.clients.delete(id));
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
      res.write('data: ' + JSON.stringify({ type: 'ready', version: 2, owner_id: owner, session: id }) + '\n\n');
      void this.pump(client); return;
    }
    if (req.method === 'POST' && req.url === '/ack') {
      const client = this.clients.get(req.headers['x-delivery-session']);
      if (!client) throw Error('unauthorized');
      await this.authorize(client.token);
      let body = ''; for await (const chunk of req) { body += chunk; if (body.length > 1024) throw Error('invalid'); }
      const value = JSON.parse(body);
      if (typeof value.task_id !== 'string' || !client.sent.has(value.task_id)) throw Error('invalid');
      this.check();
      await this.request('/api/v2/nexus/ack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ task_id: value.task_id }) });
      this.check();
      if (this.closed) throw Error('closed');
      res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end('{"ok":true}');
      client.sent.delete(value.task_id); void this.pump(client); return;
    }
    res.writeHead(404); res.end();
  }
  async pump(client) {
    if (client.flight) { client.dirty = true; return client.flight; }
    client.flight = (async () => {
      do {
        client.dirty = false;
        await this.authorize(client.token);
        const page = await this.request('/api/v2/nexus/outbox');
        this.check();
        if (this.closed || client.res.destroyed) return;
        for (const task of page.tasks) {
          if (task.owner_id !== client.owner || client.sent.has(task.task_id)) continue;
          client.sent.add(task.task_id);
          if (client.res.writableLength > 4 * 1024 * 1024) throw Error('backpressure');
          client.res.write('data: ' + JSON.stringify({ type: 'task', task }) + '\n\n');
        }
      } while (client.dirty);
    })().catch(() => client.res.destroy()).finally(() => { client.flight = null; });
    return client.flight;
  }
  wake() { for (const client of this.clients.values()) void this.pump(client); }
  close() {
    if (this.closed) return;
    this.closed = true; clearInterval(this.heartbeat); this.watcher?.close();
    for (const client of this.clients.values()) client.res.destroy();
    this.clients.clear(); this.server?.close();
    try { if (JSON.parse(fs.readFileSync(this.discoveryFile)).capability === this.capability) fs.rmSync(this.discoveryFile); } catch {}
  }
}
module.exports = { NexusEvents };

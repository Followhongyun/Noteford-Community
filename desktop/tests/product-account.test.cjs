const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { ProductAccount } = require("../electron/product-account.cjs");
function setup(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "noteford-account-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const key = crypto.randomBytes(32);
  const safeStorage = {
    isEncryptionAvailable: () => true,
    encryptString(value) {
      const iv = crypto.randomBytes(12),
        c = crypto.createCipheriv("aes-256-gcm", key, iv);
      return Buffer.concat([iv, c.update(value), c.final(), c.getAuthTag()]);
    },
    decryptString(value) {
      const d = crypto.createDecipheriv(
        "aes-256-gcm",
        key,
        value.subarray(0, 12),
      );
      d.setAuthTag(value.subarray(-16));
      return Buffer.concat([
        d.update(value.subarray(12, -16)),
        d.final(),
      ]).toString();
    },
  };
  const tokens = {
      sessionToken: "nfs_" + "s".repeat(43),
      revocationToken: "nfr_" + "r".repeat(43),
    },
    account = {
      id: "11111111-1111-4111-8111-111111111111",
      email: "new@example.test",
      displayName: null,
    };
  let online = true,
    revoked = false,
    opened = 0;
  const fetcher = async (url, options) => {
    if (!online) throw Error("offline");
    const endpoint = new URL(url).pathname.split("/noteford")[1];
    let data;
    if (endpoint === "/registration/send-code")
      data = { registrationId: crypto.randomUUID(), resendAfterSeconds: 60 };
    else if (
      ["/registration/complete", "/login", "/recovery/complete"].includes(
        endpoint,
      )
    )
      data = { ...tokens, account };
    else if (endpoint === "/me") {
      assert.equal(
        options.headers.authorization,
        "Bearer " + tokens.sessionToken,
      );
      if (revoked)
        return new Response(
          JSON.stringify({ error: { code: "unauthenticated" } }),
          { status: 401 },
        );
      data = { account };
    } else if (endpoint === "/desktop-sessions/revoke") {
      revoked = true;
      data = { revoked: true };
    }
    return new Response(
      JSON.stringify({
        version: "noteford-identity-v1",
        product: "noteford",
        ...data,
      }),
    );
  };
  const args = {
    directory,
    safeStorage,
    apiOrigin: "https://api.example",
    accountsOrigin: "https://accounts.example",
    fetcher,
    openAccounts: async () => {
      opened++;
    },
    hasLocalData: async () => true,
  };
  return {
    create: () => {
      const service = new ProductAccount(args);
      t.after(() => service.dispose());
      return service;
    },
    directory,
    account,
    tokens,
    offline: () => {
      online = false;
    },
    online: () => {
      online = true;
    },
    args,
    get opened() {
      return opened;
    },
    get revoked() {
      return revoked;
    },
  };
}

test('update credentials stay in the main process and are unavailable after logout', async t => {
  const f = setup(t), a = f.create();
  assert.equal(a.updateHeaders(), null);
  await a.submit({ mode: 'login', email: 'new@example.test', password: 'fixture-password' });
  assert.equal(a.updateHeaders().authorization, 'Bearer ' + f.tokens.sessionToken);
  assert.doesNotMatch(JSON.stringify(a.snapshot()), /nfs_|nfr_|authorization/);
  await a.logout();
  assert.equal(a.updateHeaders(), null);
});
test("persists encrypted identity and session, restores locally, and logs out without deleting local files", async (t) => {
  const f = setup(t),
    a = f.create();
  await a.restore();
  await a.submit({
    mode: "login",
    email: "new@example.test",
    password: "Test-password-1!",
  });
  assert.equal(a.snapshot().status, "signed-in");
  assert.ok(!fs.readFileSync(a.file).includes(f.tokens.sessionToken));
  assert.ok(!fs.readFileSync(a.file).includes(f.account.email));
  const b = f.create();
  assert.equal((await b.restore()).status, "signed-in");
  await b.verifySession();
  fs.writeFileSync(path.join(f.directory, "local-data"), "preserved");
  await b.logout();
  assert.equal(b.snapshot().status, "local");
  assert.ok(f.revoked);
  assert.equal(
    fs.readFileSync(path.join(f.directory, "local-data"), "utf8"),
    "preserved",
  );
  assert.equal((await f.create().restore()).status, "local");
});
test("a previously verified account opens its own workspace after a fresh offline start", async (t) => {
  const f = setup(t),
    a = f.create();
  await a.submit({
    mode: "login",
    email: "new@example.test",
    password: "Test-password-1!",
  });
  f.offline();
  a.dispose();
  let openedAccount;
  f.args.activate = async account => { openedAccount = account.id; return {epoch:'offline'}; };
  const b = f.create();
  assert.equal((await b.restore()).status, "signed-in");
  await b.verifySession();
  assert.equal(b.snapshot().account.id, f.account.id);
  assert.equal(openedAccount, f.account.id);
  f.online();
  await b.verifySession();
  assert.equal(b.snapshot().status, "signed-in");
  assert.equal(b.snapshot().workspaceEpoch, "offline");
});
test("offline logout persists revocation queue and never restores the old session", async (t) => {
  const f = setup(t),
    a = f.create();
  await a.submit({
    mode: "login",
    email: "new@example.test",
    password: "Test-password-1!",
  });
  f.offline();
  await a.logout();
  const restored = f.create();
  assert.equal((await restored.restore()).status, "local");
  await restored.flushRevocations();
  f.online();
  assert.equal((await f.create().restore()).status, "local");
  assert.ok(f.revoked);
});
test("fails closed before creating a session when encrypted storage is unavailable", async (t) => {
  const f = setup(t),
    a = f.create();
  f.args.safeStorage.isEncryptionAvailable = () => false;
  await a.submit({
    mode: "login",
    email: "new@example.test",
    password: "Test-password-1!",
  });
  assert.equal(a.snapshot().status, "signed-out");
  assert.match(a.snapshot().error, /安全保存/);
});
test("cancel during exchange does not sign in and revokes any issued credential", async (t) => {
  const f = setup(t),
    a = f.create();
  const polling = a.submit({
    mode: "login",
    email: "new@example.test",
    password: "Test-password-1!",
  });
  a.cancel();
  await polling;
  assert.equal(a.snapshot().status, "signed-out");
  assert.ok(f.revoked);
});

test('anonymous startup activates a local workspace before publishing content and never authorizes updates', async t => {
  const f = setup(t); let release; let identity = 'unset';
  f.args.activate = account => new Promise(resolve => { identity = account; release = () => resolve({epoch:'local'}); });
  const a = f.create(), restoring = a.restore();
  while (!release) await new Promise(resolve => setImmediate(resolve));
  assert.notEqual(a.snapshot().status, 'local');
  assert.equal(identity, null);
  release(); await restoring;
  assert.equal(a.snapshot().status, 'local');
  assert.equal(a.updateHeaders(), null);
  await assert.rejects(a.bridgeIdentity(), /unauthenticated/);
  assert.equal((await a.restore()).workspaceEpoch, 'local');
  a.cancel();
  assert.equal((await a.restore()).status, 'signed-out');
});

test('cancel during workspace activation cannot re-open data or publish signed-in', async t => {
  const f = setup(t); let release; let open = false;
  f.args.activate = () => new Promise(resolve => { release = () => { open = true; resolve({epoch: 'late'}); }; });
  f.args.deactivate = () => { open = false; };
  const a = f.create(), login = a.submit({mode: 'login', password: 'test'});
  while (!release) await new Promise(resolve => setImmediate(resolve));
  a.cancel(); release(); await login;
  assert.equal(open, false); assert.equal(a.snapshot().status, 'signed-out');
});

test("bridge identity is reverified and revoked sessions synchronously close access", async t => {
  const f=setup(t),a=f.create();
  await a.submit({mode:'login',email:f.account.email,password:'temporary-fixture-password'});
  assert.deepEqual(await a.bridgeIdentity(),{id:f.account.id});
  await f.args.fetcher('https://api.example/v2/products/noteford/desktop-sessions/revoke',{});
  await assert.rejects(a.bridgeIdentity(),/unauthenticated/);
  assert.equal(a.snapshot().status,'signed-out');
});
test("late identity verification cannot reauthenticate after logout", async t => {
  const f=setup(t),a=f.create();
  await a.submit({mode:'login',email:f.account.email,password:'temporary-fixture-password'});
  const original=a.fetcher;let finish,started;
  const pending=new Promise(resolve=>{started=resolve});
  a.fetcher=async (url,options)=>{if(url.endsWith('/me')){started();return new Promise(resolve=>{finish=resolve});}return original(url,options);};
  const checking=a.bridgeIdentity();await pending;await a.logout();
  finish(Response.json({version:'noteford-identity-v1',product:'noteford',account:f.account}));
  await assert.rejects(checking,/unauthenticated/);assert.equal(a.snapshot().status,'local');
});

test('concurrent restores share the final result instead of returning a stale loading state', async t => {
  const f = setup(t), writer = f.create();
  await writer.submit({mode:'register',email:f.account.email,password:'test'});
  const a = f.create(), original = a.fetcher;
  let release, started, calls = 0;
  const entered = new Promise(resolve => { started = resolve; });
  a.fetcher = async (url, options) => {
    if (url.endsWith('/me')) { calls++; started(); await new Promise(resolve => { release = resolve; }); }
    return original(url, options);
  };
  const first = a.restore(); await entered;
  const second = a.restore();
  release();
  assert.equal((await first).status, 'signed-in');
  assert.equal((await second).status, 'signed-in');
  await a.verifySession();
  assert.equal(calls, 1);
});

test('local startup does not wait for hanging authentication or pending old revocations', async t => {
  const f = setup(t), writer = f.create();
  await writer.submit({mode:'login',email:f.account.email,password:'test'});
  writer.record.revocations.push('nfr_' + 'p'.repeat(43)); writer.save(); writer.dispose();
  const pending = [];
  f.args.fetcher = (url) => new Promise(resolve => pending.push({url, resolve}));
  const reader = f.create();
  const result = await Promise.race([
    reader.restore(),
    new Promise((_, reject) => {
      const timer = setTimeout(() => reject(Error('startup waited on network')), 500);
      timer.unref();
    }),
  ]);
  assert.equal(result.status, 'signed-in');
  assert.equal(result.account.id, f.account.id);
  assert.equal(pending.length, 2);
  for (const request of pending) request.resolve(Response.json({
    version:'noteford-identity-v1', product:'noteford', account:f.account, revoked:true,
  }));
  await Promise.all([reader.verifySession(), reader.flushRevocations()]);
});

test('legacy encrypted token migrates only after online validation, then supports offline restart', async t => {
  const f = setup(t), writer = f.create();
  await writer.submit({mode:'login',email:f.account.email,password:'test'});
  delete writer.record.session.account; delete writer.record.session.apiOrigin;
  writer.save(); writer.dispose();
  f.offline();
  const reader = f.create();
  assert.equal((await reader.restore()).status, 'error');
  assert.equal(reader.snapshot().account, null);
  f.online();
  assert.equal((await reader.restore()).status, 'signed-in');
  reader.dispose(); f.offline();
  const restarted = f.create();
  assert.equal((await restarted.restore()).status, 'signed-in');
  await restarted.verifySession();
});

test('encrypted cache with invalid identity or a different API origin cannot open any workspace', async t => {
  const f = setup(t), writer = f.create();
  await writer.submit({mode:'login',email:f.account.email,password:'test'});
  writer.dispose(); f.offline();
  let activations = 0;
  f.args.activate = async () => { activations++; };
  writer.record.session.apiOrigin = 'https://another.example'; writer.save();
  assert.equal((await f.create().restore()).status, 'error');
  writer.record.session.apiOrigin = f.args.apiOrigin;
  writer.record.session.account = {...f.account, id:'../another-account'}; writer.save();
  assert.equal((await f.create().restore()).status, 'error');
  assert.equal(activations, 0);
});

test('background revocation locks local access and removes remembered identity from disk', async t => {
  const f = setup(t); let active = false;
  f.args.activate = async () => { active = true; return {epoch:'revoked'}; };
  f.args.deactivate = () => { active = false; };
  const writer = f.create();
  await writer.submit({mode:'login',email:f.account.email,password:'test'});
  await f.args.fetcher('https://api.example/v2/products/noteford/desktop-sessions/revoke',{});
  const reader = f.create();
  await reader.restore(); await reader.verifySession();
  assert.equal(reader.snapshot().status, 'signed-out');
  assert.equal(active, false);
  assert.equal(reader.record.session, undefined);
  f.offline();
  assert.equal((await f.create().restore()).status, 'local');
});

test('late background revocation from a previous account cannot log out a newly signed-in account', async t => {
  const f = setup(t), a = f.create();
  await a.submit({mode:'login',email:f.account.email,password:'test'});
  const original = a.fetcher; let finish;
  a.fetcher = (url, options) => url.endsWith('/me')
    ? new Promise(resolve => { finish = resolve; }) : original(url, options);
  const checking = a.verifySession();
  await a.logout();
  const other = {...f.account, id:'22222222-2222-4222-8222-222222222222', email:'other@example.test'};
  a.fetcher = async () => Response.json({version:'noteford-identity-v1',product:'noteford',account:other,...f.tokens});
  await a.submit({mode:'login',email:other.email,password:'test'});
  finish(Response.json({error:{code:'unauthenticated'}},{status:401}));
  await checking;
  assert.equal(a.snapshot().status, 'signed-in');
  assert.equal(a.snapshot().account.id, other.id);
  assert.equal(a.record.session.account.id, other.id);
});

test('successful background validation updates metadata without reopening the workspace', async t => {
  const f = setup(t); let activations = 0;
  f.args.activate = async () => ({epoch:++activations});
  const a = f.create();
  await a.submit({mode:'login',email:f.account.email,password:'test'});
  a.fetcher = async () => Response.json({version:'noteford-identity-v1',product:'noteford',
    account:{...f.account,displayName:'New name'}});
  await a.verifySession();
  assert.equal(a.snapshot().account.displayName, 'New name');
  assert.equal(a.snapshot().workspaceEpoch, 1);
  assert.equal(activations, 1);
});

test('renderer remount after login never closes an authenticated workspace or repeats authentication', async t => {
  const f = setup(t); let closed = 0, activated = 0;
  f.args.deactivate = async () => { closed++; };
  f.args.activate = async () => { activated++; return {epoch:'stable-epoch'}; };
  const a = f.create(); await a.submit({mode:'login',email:f.account.email,password:'test'});
  const previousClosed = closed;
  a.fetcher = async () => { throw Error('unexpected network request'); };
  for (let i = 0; i < 3; i++) {
    const state = await a.restore();
    assert.equal(state.status,'signed-in'); assert.equal(state.workspaceEpoch,'stable-epoch');
  }
  assert.equal(closed,previousClosed); assert.equal(activated,1);
});

test('registration followed by workspace failure retains its session and recovers without consuming another invite', async t => {
  const f = setup(t); let available = false, registrations = 0;
  const original = f.args.fetcher;
  f.args.fetcher = async (url, options) => {
    if (url.endsWith('/registration/complete')) registrations++;
    return original(url, options);
  };
  f.args.activate = async () => { if (!available) throw Error('startup failed'); return {epoch:'recovered'}; };
  const a = f.create();
  assert.equal((await a.submit({mode:'register',email:f.account.email,password:'test'})).status,'signed-out');
  assert.match(a.snapshot().error,/资料库/);
  available = true;
  assert.equal((await a.restore()).status,'signed-in');
  assert.equal(registrations,1);
});


test("failed mail keeps only a validated challenge for the user's retry flow", async t => {
  const env = setup(t), id = crypto.randomUUID();
  const app = new ProductAccount({...env.args, fetcher: async () => new Response(JSON.stringify({version: "noteford-identity-v1", registrationId: id, error: {code: "email_unavailable"}}), {status: 503})});
  const result = await app.sendCode("new@example.test");
  assert.equal(result.registrationId, id);
  assert.equal(result.resendAfterSeconds, 60);
  assert.match(result.error, /暂时发送失败/);
});

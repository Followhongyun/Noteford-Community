"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { validateAccount } = require("./product-workspace.cjs");
const version = "noteford-identity-v1";
const messages = {
  recovery_retry: "新密码未设置成功，请重新获取邮箱验证码后重试。",
  code_invalid: "邮箱验证码不正确，请检查后重试。",
  code_expired: "邮箱验证码已过期，请重新发送。",
  code_locked: "验证码尝试次数过多，请重新发送。",
  registration_pending:
    "注册正在确认中，请保留填写内容后重试；如果已设置过密码，也可切换到登录继续。",
  registration_changed:
    "这次注册正在恢复，请使用原先填写的内容重试，或使用邮箱和原密码登录。",
  account_exists:
    "这个邮箱已经注册，请切换到登录，无需邀请码。",
  invalid_credentials: "邮箱或密码不正确，请检查后重试。",
  email_unconfirmed: "这个邮箱尚未验证，请在应用内使用“忘记密码”完成邮箱验证。",
  weak_password:
    "密码未通过账号服务的安全检查，请换一个未在其他网站用过的长密码。",
  email_unavailable: "验证码邮件暂时发送失败，请稍后重试。",
  invalid_request: "填写内容有误，请检查后重试。",
  invite_invalid: "邀请码不正确，请检查后重试。",
  invite_expired: "邀请码已过期，请联系邀请人获取新码。",
  invite_used: "邀请码已用完，请联系邀请人获取新码。",
  invite_required: "注册需要邀请码，请填写后重试。",
  unauthenticated: "登录已过期，请重新登录。",
  forbidden: "这个账号暂时无法使用 Noteford，请联系邀请人。",
  expired: "登录等待时间已结束，请重试。",
  conflict: "这次登录已结束，请重新开始。",
  consumed: "这次登录已结束，请重新开始。",
  access_denied: "登录已取消，你可以重新开始。",
  rate_limited: "操作太频繁，请稍后重试。",
  network: "暂时无法连接账号服务。请检查网络或代理连接后重试；已完成的注册无需重新注册。",
  storage: "无法安全保存登录，请检查系统钥匙串后重试。",
  workspace: "资料库暂时无法安全打开，请重试或重新启动应用。",
  unconfigured: "账号服务暂未开放，请稍后重试。",
};
function origin(value, development) {
  const u = new URL(value);
  if (
    u.origin !== value ||
    u.username ||
    u.password ||
    !(
      u.protocol === "https:" ||
      (development && u.protocol === "http:" && u.hostname === "127.0.0.1")
    )
  )
    throw Error("unconfigured");
  return u.origin;
}
class ProductAccount {
  constructor({
    directory,
    safeStorage,
    apiOrigin,
    development = false,
    fetcher = fetch,
    changed = () => {},
    activate = async () => ({}),
    deactivate = async () => {},
    appVersion = "0.1.5",
    platform = process.platform,
  }) {
    Object.assign(this, {
      directory,
      safeStorage,
      fetcher,
      changed,
      activate,
      deactivate,
      appVersion,
      platform,
    });
    try {
      this.apiOrigin = origin(apiOrigin, development);
    } catch {}
    this.file = path.join(directory, "product-account.enc");
    this.generation = 0;
    this.busy = false;
    this.closing = Promise.resolve();
    this.record = { installationId: randomUUID(), revocations: [] };
    this.state = {
      status: "restoring",
      account: null,
      error: null,
      canUseLocal: false,
    };
  }
  snapshot() {
    return { ...this.state };
  }
  emit(patch) {
    this.state = { ...this.state, ...patch };
    this.changed(this.snapshot());
    return this.snapshot();
  }
  save() {
    try {
      if (
        !this.safeStorage.isEncryptionAvailable() ||
        this.safeStorage.getSelectedStorageBackend?.() === "basic_text"
      )
        throw Error();
      fs.mkdirSync(this.directory, { recursive: true, mode: 0o700 });
      const data = this.safeStorage.encryptString(JSON.stringify(this.record));
      fs.writeFileSync(this.file + ".tmp", data, { mode: 0o600 });
      fs.renameSync(this.file + ".tmp", this.file);
    } catch {
      throw Error("storage");
    }
  }
  async request(endpoint, body, token) {
    if (!this.apiOrigin) throw Error("unconfigured");
    let response;
    try {
      response = await this.fetcher(
        this.apiOrigin + "/v2/products/noteford" + endpoint,
        {
          method: body ? "POST" : "GET",
          headers: {
            "content-type": "application/json",
            ...(token ? { authorization: "Bearer " + token } : {}),
          },
          ...(body ? { body: JSON.stringify({ version, ...body }) } : {}),
          redirect: "error",
          signal: AbortSignal.timeout(12000),
        },
      );
    } catch {
      throw Error("network");
    }
    let value;
    try {
      const text = await response.text();
      if (text.length > 16384) throw Error();
      value = JSON.parse(text);
    } catch {
      throw Error("network");
    }
    if (!response.ok) {
      const error = Error(value?.error?.code === "authorization_pending" ? "authorization_pending" : Object.hasOwn(messages, value?.error?.code) ? value.error.code : "network");
      if (error.message === "email_unavailable" && value.version === version && ["/registration/send-code", "/recovery/send-code"].includes(endpoint)) {
        const key = endpoint.startsWith("/recovery/") ? "recoveryId" : "registrationId";
        if (typeof value[key] === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value[key])) error.challenge = {[key]: value[key], resendAfterSeconds: 60};
      }
      throw error;
    }
    if (value.version !== version || value.product !== "noteford")
      throw Error("network");
    return value;
  }
  flushRevocations() {
    if (this.revocationTask) return this.revocationTask;
    this.revocationTask = this.flushRevocationsOnce().finally(() => {
      this.revocationTask = null;
    });
    return this.revocationTask;
  }
  async flushRevocationsOnce() {
    for (const token of [...this.record.revocations]) {
      try {
        await this.request("/desktop-sessions/revoke", {
          revocationToken: token,
        });
        this.record.revocations = this.record.revocations.filter(
          (x) => x !== token,
        );
        this.save();
      } catch {
        break;
      }
    }
  }
  restore() {
    if (this.explicitAuth) return Promise.resolve(this.snapshot());
    // Renderer remounts must not tear down an already authenticated workspace.
    if (["signed-in", "local"].includes(this.state.status)) return Promise.resolve(this.snapshot());
    if (this.restoreTask) return this.restoreTask;
    const task = this.restoreOnce();
    this.restoreTask = task.finally(() => { this.restoreTask = null; });
    return this.restoreTask;
  }
  async restoreOnce() {
    if (this.busy) return this.snapshot();
    this.busy = true;
    const generation = this.generation;
    try {
      await this.closeWorkspace();
      if (generation !== this.generation) return this.snapshot();
      this.emit({
        status: "restoring",
        error: null,
        canUseLocal: false,
        account: null,
      });
      if (fs.existsSync(this.file)) {
        try {
          const data = JSON.parse(
            this.safeStorage.decryptString(fs.readFileSync(this.file)),
          );
          if (!data.installationId || !Array.isArray(data.revocations))
            throw Error();
          this.record = data;
        } catch {
          throw Error("storage");
        }
      }
      // Old logout requests must not delay opening this account's local data.
      void this.flushRevocations();
      if (!this.record.session)
        return await this.enterLocal(generation);
      const session = this.record.session;
      if (!/^nfs_[A-Za-z0-9_-]{43}$/.test(session.sessionToken)
          || !/^nfr_[A-Za-z0-9_-]{43}$/.test(session.revocationToken))
        throw Error("storage");
      try {
        // Only trust identity saved with the credential in OS-encrypted storage.
        // A token-only record from an older release needs one online migration.
        if (session.account) {
          validateAccount(session.account);
          if (session.apiOrigin !== this.apiOrigin) throw Error("storage");
          const state = await this.enter(session.account, generation);
          if (this.isCurrentSession(session, generation))
            void this.verifySession();
          return state;
        }
        const result = await this.request(
          "/me",
          null,
          session.sessionToken,
        );
        if (generation !== this.generation) return this.snapshot();
        validateAccount(result.account);
        session.account = result.account;
        session.apiOrigin = this.apiOrigin;
        this.save();
        const state = await this.enter(result.account, generation);
        this.scheduleVerification(session, generation);
        return state;
      } catch (error) {
        if (generation !== this.generation) return this.snapshot();
        if (["unauthenticated", "forbidden", "expired"].includes(error.message)) {
          delete this.record.session;
          this.save();
          return this.emit({
            status: "signed-out",
            account: null,
            error: messages[error.message],
          });
        }
        throw error;
      }
    } catch (error) {
      if (generation !== this.generation) return this.snapshot();
      return this.emit({
        status: "error",
        account: null,
        error: messages[error.message] || messages.network,
      });
    } finally {
      this.busy = false;
    }
  }
  async sendCode(email, purpose = "register") {
    try {
      return await this.request(
        purpose === "recover"
          ? "/recovery/send-code"
          : "/registration/send-code",
        { email: String(email).trim().toLowerCase() },
      );
    } catch (error) {
      return { ...(error.challenge ?? {}), error: messages[error.message] || messages.network };
    }
  }
  async submit(input) {
    if (this.busy) return this.snapshot();
    this.busy = true;
    this.stopVerification();
    const generation = ++this.generation;
    try {
      await this.closeWorkspace();
      this.save();
      const fields = {
        email: String(input.email || "")
          .trim()
          .toLowerCase(),
        password: String(input.password || ""),
        installationId: this.record.installationId,
      };
      const result =
        input.mode === "recover"
          ? await this.request("/recovery/complete", {
              ...fields,
              recoveryId: input.registrationId,
              code: input.code,
            })
          : input.mode === "register"
            ? await this.request("/registration/complete", {
                ...fields,
                registrationId: input.registrationId,
                code: input.code,
                displayName: input.displayName,
              })
            : await this.request("/login", fields);
      if (generation !== this.generation) {
        await this.request("/desktop-sessions/revoke", {
          revocationToken: result.revocationToken,
        }).catch(() => {});
        return this.snapshot();
      }
      if (
        !/^nfs_[A-Za-z0-9_-]{43}$/.test(result.sessionToken) ||
        !/^nfr_[A-Za-z0-9_-]{43}$/.test(result.revocationToken) ||
        !result.account?.id ||
        !result.account?.email
      )
        throw Error("network");
      validateAccount(result.account);
      this.record.session = {
        sessionToken: result.sessionToken,
        revocationToken: result.revocationToken,
        account: result.account,
        apiOrigin: this.apiOrigin,
      };
      try {
        this.save();
      } catch (error) {
        delete this.record.session;
        await this.request("/desktop-sessions/revoke", {
          revocationToken: result.revocationToken,
        }).catch(() => {});
        throw error;
      }
      const state = await this.enter(result.account, generation);
      this.scheduleVerification(this.record.session, generation);
      return state;
    } catch (error) {
      if (generation !== this.generation) return this.snapshot();
      return this.emit({
        status: "signed-out",
        account: null,
        error: messages[error.message] || messages.network,
      });
    } finally {
      this.busy = false;
    }
  }
  closeWorkspace() {
    const closing = this.deactivate(); // lock synchronously before any awaits
    this.closing = Promise.all([this.closing, closing]).then(() => undefined);
    return this.closing;
  }
  async enter(account, generation) {
    this.explicitAuth = false;
    validateAccount(account);
    let workspace;
    try { workspace = await this.activate(account); }
    catch { await this.closeWorkspace(); throw Error("workspace"); }
    if (generation !== this.generation) {
      await this.closeWorkspace();
      return this.snapshot();
    }
    return this.emit({ status: "signed-in", account, error: null, workspaceEpoch: workspace?.epoch, legacyWorkspace: workspace?.legacy === true });
  }
  isCurrentSession(session, generation) {
    return generation === this.generation && this.record.session === session
      && this.state.status === "signed-in";
  }
  stopVerification() {
    clearTimeout(this.verificationTimer);
    this.verificationTimer = null;
  }
  scheduleVerification(session, generation, delay = 5 * 60 * 1000) {
    if (!this.isCurrentSession(session, generation)) return;
    this.stopVerification();
    this.verificationTimer = setTimeout(() => { void this.verifySession(); }, delay);
    this.verificationTimer.unref?.();
  }
  invalidateSession(session, generation, code) {
    if (!this.isCurrentSession(session, generation)) return;
    delete this.record.session;
    let error = messages[code];
    try { this.save(); } catch { error = messages.storage; }
    this.cancel(); // Locks local access before any asynchronous teardown.
    this.emit({ error });
  }
  verifySession() {
    const session = this.record.session, generation = this.generation;
    if (!session || !this.isCurrentSession(session, generation))
      return Promise.resolve(this.snapshot());
    if (this.verification?.session === session && this.verification.generation === generation)
      return this.verification.task;
    this.stopVerification();
    const verification = { session, generation };
    verification.task = this.verifySessionOnce(session, generation).finally(() => {
      if (this.verification === verification) this.verification = null;
    });
    this.verification = verification;
    return verification.task;
  }
  async verifySessionOnce(session, generation) {
    let retryDelay = 5 * 60 * 1000;
    try {
      const result = await this.request("/me", null, session.sessionToken);
      if (!this.isCurrentSession(session, generation)) return this.snapshot();
      if (validateAccount(result.account) !== validateAccount(session.account))
        throw Error("unauthenticated");
      session.account = result.account;
      this.save();
      // Refresh profile metadata without reopening the workspace or changing its lease.
      this.emit({ account: result.account, error: null });
    } catch (error) {
      if (!this.isCurrentSession(session, generation)) return this.snapshot();
      if (["unauthenticated", "forbidden", "expired"].includes(error.message)) {
        this.invalidateSession(session, generation, error.message);
      } else {
        // Network, proxy and service failures are not a logout instruction.
        retryDelay = 30 * 1000;
      }
    } finally {
      this.scheduleVerification(session, generation, retryDelay);
    }
    return this.snapshot();
  }
  dispose() {
    this.stopVerification();
    this.generation++;
  }
  cancel() {
    this.explicitAuth = true;
    this.stopVerification();
    this.generation++;
    this.pending = null;
    void this.closeWorkspace().catch(() => {});

    return this.emit({ status: "signed-out", account: null, error: null });
  }
  async logout() {
    this.cancel();
    if (this.record.session) {
      this.record.revocations.push(this.record.session.revocationToken);
      delete this.record.session;
      try {
        this.save();
      } catch {
        this.emit({ status: "error", error: messages.storage });
        return this.snapshot();
      }
    }
    this.emit({ status: "signed-out", account: null });
    await this.closing;
    void this.flushRevocations();
    return this.local();
  }
  updateHeaders() {
    // Main-process only; never exposed through account snapshots or IPC.
    const session = this.record.session;
    if (this.state.status !== 'signed-in' || !session
        || session.apiOrigin !== this.apiOrigin) return null;
    return { authorization: `Bearer ${session.sessionToken}` };
  }
  async bridgeIdentity() {
    const generation = this.generation;
    const session = this.record.session;
    if (this.state.status !== 'signed-in' || !session) throw Error('unauthenticated');
    try {
      const result = await this.request('/me', null, session.sessionToken);
      const id = validateAccount(result.account);
      if (generation !== this.generation || this.state.status !== 'signed-in'
          || this.record.session !== session || id !== validateAccount(this.state.account)) throw Error('unauthenticated');
      return { id };
    } catch (error) {
      if (['unauthenticated', 'forbidden', 'expired'].includes(error.message))
        this.invalidateSession(session, generation, error.message);
      throw Error('unauthenticated');
    }
  }
  async enterLocal(generation) {
    this.explicitAuth = false;
    let workspace;
    try { workspace = await this.activate(null); }
    catch { await this.closeWorkspace(); throw Error("workspace"); }
    if (generation !== this.generation) {
      await this.closeWorkspace();
      return this.snapshot();
    }
    return this.emit({ status: "local", account: null, error: null,
      canUseLocal: true, workspaceEpoch: workspace?.epoch, legacyWorkspace: false });
  }
  async local() {
    if (this.busy) return this.snapshot();
    this.busy = true;
    this.stopVerification();
    const generation = ++this.generation;
    try {
      await this.closeWorkspace();
      if (this.record.session) {
        this.record.revocations.push(this.record.session.revocationToken);
        delete this.record.session;
        this.save();
        void this.flushRevocations();
      }
      return await this.enterLocal(generation);
    } catch (error) {
      return this.emit({ status: "error", account: null, error: messages[error.message] || messages.workspace });
    } finally { this.busy = false; }
  }
}
module.exports = { ProductAccount, messages, origin };

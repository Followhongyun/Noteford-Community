import {
  Fragment,
  createContext,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import type { ProductAccountState } from "../types";
import Icon from "./Icon";
import notefordIcon from "../assets/noteford.png";
// Auth card structure adapted from shadcn/ui login-03 (MIT).
// Source: https://github.com/shadcn-ui/ui/tree/main/apps/v4/registry/new-york-v4/blocks/login-03
// License: desktop/licenses/shadcn-ui-MIT.txt

const initial: ProductAccountState = {
  status: "restoring",
  account: null,
  error: null,
  canUseLocal: false,
};
type Mode = "login" | "register" | "recover";
const AccountContext = createContext<{
  state: ProductAccountState;
  login(): void;
  logout(): Promise<void>;
}>({ state: initial, login() {}, async logout() {} });

function PasswordField({
  label,
  value,
  onChange,
  disabled,
  current = false,
  error,
}: {
  label: string;
  value: string;
  onChange(value: string): void;
  disabled: boolean;
  current?: boolean;
  error?: string;
}) {
  const id = useId(),
    [visible, setVisible] = useState(false),
    [capsLock, setCapsLock] = useState(false);
  return (
    <div className="product-field">
      <label htmlFor={id}>{label}</label>
      <div className="product-password-input">
        <input
          id={id}
          type={visible ? "text" : "password"}
          autoComplete={current ? "current-password" : "new-password"}
          placeholder={current ? "输入你的密码" : label === "确认密码" ? "再输入一次密码" : "设置密码，至少 8 位"}
          required
          minLength={current ? 1 : 8}
          maxLength={256}
          value={value}
          disabled={disabled}
          aria-invalid={!!error}
          aria-describedby={
            error ? `${id}-error` : capsLock ? `${id}-caps` : undefined
          }
          onChange={(event) => onChange(event.target.value)}
          onKeyUp={(event) => setCapsLock(event.getModifierState("CapsLock"))}
          onKeyDown={(event) => setCapsLock(event.getModifierState("CapsLock"))}
          onBlur={() => setCapsLock(false)}
        />
        <button
          type="button"
          disabled={disabled}
          aria-label={`${visible ? "隐藏" : "显示"}${label}`}
          title={`${visible ? "隐藏" : "显示"}${label}`}
          aria-pressed={visible}
          onClick={() => setVisible((old) => !old)}
        >
          <Icon name={visible ? "eyeOff" : "eye"} size={17} />
        </button>
      </div>
      {error && (
        <span className="product-field-error" id={`${id}-error`}>
          {error}
        </span>
      )}
      {capsLock && (
        <span className="product-field-hint" id={`${id}-caps`}>
          大写锁定已开启
        </span>
      )}
    </div>
  );
}

export function useProductAccount() { return useContext(AccountContext).state }

export function ProductAccountSwitch() {
  const { state, login, logout } = useContext(AccountContext);
  const [busy, setBusy] = useState(false);
  return <div className="product-account-switch">
    <span>{state.account?.email || "本地使用"}</span>
    <button type="button" disabled={busy} onClick={async () => {
      setBusy(true);
      try { if (state.account) await logout(); else login(); } finally { setBusy(false); }
    }}>{busy ? '正在退出…' : state.account ? '退出账号' : '登录以自动更新'}</button>
  </div>;
}

export function ProductAccountSettings({ embedded = false }: { embedded?: boolean }) {
  const { state, login, logout } = useContext(AccountContext),
    [busy, setBusy] = useState(false);
  return (
    <section className={embedded ? "settings-account-embedded" : "settings-group"} aria-label="Followhongyun 账号">
      {!embedded && <h2>Followhongyun 账号</h2>}
      <div className="preference-row">
        <div className="preference-label">
          <h3>{state.account?.email || "尚未登录"}</h3>
          <p className="preference-description">
            {state.account
              ? "已启用自动更新。账号资料在退出后仍会保留。"
              : "无需登录即可使用；登录后启用自动更新。本地资料与账号资料分别保存。"}
          </p>
        </div>
        <div className="preference-control">
          {state.account ? (
            <button
              className="settings-button"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await logout();
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? "正在退出…" : "退出账号"}
            </button>
          ) : (
            <button className="settings-button" onClick={login}>
              登录 Noteford
            </button>
          )}
        </div>
      </div>
    </section>
  );
}

export default function ProductAccount({ children }: { children: ReactNode }) {
  const [state, setState] = useState(initial),
    [mode, setMode] = useState<Mode>("login");
  const [lastIdentity, setLastIdentity] = useState<ProductAccountState["account"]>(null);
  useEffect(() => {
    if (state.account) setLastIdentity(state.account);
  }, [state.account]);
  const [fields, setFields] = useState({
    displayName: "",
    email: "",
    code: "",
    password: "",
    confirm: "",
  });
  const [challenge, setChallenge] = useState<{
    id: string;
    email: string;
    mode: Mode;
  } | null>(null);
  const [remaining, setRemaining] = useState(0),
    [sending, setSending] = useState(false),
    [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(""),
    [failed, setFailed] = useState(false);
  const pending = useRef(false),
    deadline = useRef(0),
    cooldown = remaining > 0;
  useEffect(() => {
    const off = window.noteford.onProductAccount(setState);
    void window.noteford
      .productAccount("restore")
      .then(setState)
      .catch(() =>
        setState({
          ...initial,
          status: "error",
          error: "无法读取登录状态，请重试。",
        }),
      );
    return off;
  }, []);
  useEffect(() => {
    if (!cooldown) return;
    const timer = setInterval(
      () =>
        setRemaining(
          Math.max(0, Math.ceil((deadline.current - Date.now()) / 1000)),
        ),
      250,
    );
    return () => clearInterval(timer);
  }, [cooldown]);
  function edit(key: keyof typeof fields, value: string) {
    setFields((old) => ({ ...old, [key]: value }));
    setNotice("");
    setState((old) => ({ ...old, error: null }));
    if (key === "email") setChallenge(null);
  }
  function changeMode(next: Mode) {
    setMode(next);
    setNotice("");
    setFailed(false);
    setState((old) => ({ ...old, error: null }));
    // Preserve filled fields when switching; a code is valid only for its issuing flow.
  }
  async function action(name: "restore" | "cancel" | "logout" | "local") {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setNotice("");
    try {
      setState(await window.noteford.productAccount(name));
    } catch {
      setNotice("操作未完成，请重试。");
      setFailed(true);
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  function problem(message: string) {
    setNotice(message);
    setFailed(true);
  }
  async function send() {
    if (pending.current || remaining) return;
    const email = fields.email.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      problem("请先填写有效的邮箱地址。");
      return;
    }
    pending.current = true;
    setSending(true);
    setNotice("");
    setFailed(false);
    try {
      const result = await window.noteford.sendProductCode(
        email,
        mode === "recover" ? "recover" : "register",
      );
      const id = result.registrationId ?? result.recoveryId;
      if (result.error) {
        if (id) {
          setChallenge({id, email, mode});
          deadline.current = Date.now() + (result.resendAfterSeconds ?? 60) * 1000;
          setRemaining(result.resendAfterSeconds ?? 60);
        }
        problem(result.error + (id ? "如果已收到邮件或管理员重试后收到，可直接填写验证码继续；未收到可在倒计时结束后重发。" : ""));
        return;
      }
      if (!id) {
        problem("验证码发送未完成，请重试。");
        return;
      }
      setChallenge({ id, email, mode });
      deadline.current = Date.now() + (result.resendAfterSeconds ?? 60) * 1000;
      setRemaining(result.resendAfterSeconds ?? 60);
      setNotice(
        mode === "recover"
          ? "如果该邮箱已注册，验证码将发到你的邮箱。请检查收件箱和垃圾邮件；未收到可在倒计时结束后重发。"
          : "验证码已发出，请检查收件箱和垃圾邮件。未收到可在倒计时结束后重发，已填写的内容会保留。",
      );
    } catch {
      problem("验证码发送失败，请检查网络后重试。");
    } finally {
      pending.current = false;
      setSending(false);
    }
  }
  const mismatch = !!fields.confirm && fields.password !== fields.confirm;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending.current) return;
    const email = fields.email.trim().toLowerCase();
    if (mode !== "login") {
      if (!challenge || challenge.email !== email || challenge.mode !== mode) {
        problem("请先发送并填写这个邮箱的验证码。");
        return;
      }
      if (fields.password !== fields.confirm) {
        problem("两次输入的密码不一致。");
        return;
      }
    }
    pending.current = true;
    setBusy(true);
    setNotice("");
    setFailed(false);
    try {
      const next = await window.noteford.submitProductAccount({
        mode,
        email,
        password: fields.password,
        ...(mode === "register"
          ? {
              displayName: fields.displayName.trim(),
            }
          : {}),
        ...(mode !== "login"
          ? { code: fields.code, registrationId: challenge?.id }
          : {}),
      });
      setState(next);
      if (next.status === "signed-in") {
        setFields((old) => ({ ...old, password: "", confirm: "", code: "" }));
        setChallenge(null);
      }
    } catch {
      problem("连接中断，请保留填写内容后重试。");
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  const content = state.status === "signed-in" || state.status === "local",
    disabled = busy || sending;
  return (
    <AccountContext.Provider
      value={{
        state,
        login: () => void action("cancel"),
        logout: () => action("logout"),
      }}
    >
      {content ? (
        <Fragment key={state.account?.id}>{children}</Fragment>
      ) : (
        <main className="product-welcome">
          <div className="product-welcome-drag" />
          <section
            className="product-welcome-card"
            aria-labelledby="product-welcome-title"
          >
            <header className="product-auth-header">
              <img className="product-app-icon" src={notefordIcon} alt="Noteford 图标" width={64} height={64} />
              <span className="product-brand-name">Noteford</span>
              <h1 id="product-welcome-title">
                {mode === "register" ? `Hi${fields.displayName.trim() ? ` ${fields.displayName.trim()}` : ""}！很高兴认识你`  : mode === "recover" ? "找回你的密码" : <>
                  <span className="product-login-welcome">欢迎回来</span>
                  {(state.account || lastIdentity)?.displayName && (!fields.email.trim() || fields.email.trim().toLowerCase() === (state.account || lastIdentity)?.email.toLowerCase()) && (
                    <span className="product-login-nickname">{(state.account || lastIdentity)?.displayName}</span>
                  )}
                </>}
              </h1>
              <p className="product-welcome-description">
                {mode === "register" ? <><span className="product-tagline-line">从 Noteford 开始，</span><span className="product-tagline-line">Follow洪云，让默契自然发生。</span></> : mode === "recover" ? "验证 Followhongyun 账号邮箱，设置新密码。" : "登录 Followhongyun 账号，即可启用自动更新。"}
              </p>
            </header>
            <div className="product-auth-body">
            {state.status !== "restoring" && <button type="button" className="product-secondary" disabled={busy || sending}
              onClick={() => void action("local")}>不登录，直接使用</button>}
            {state.status === "restoring" ? (
              <div role="status" aria-live="polite">
                <p>正在恢复登录…</p>
                <p className="product-field-hint">正在打开资料库，请稍候。</p>
                <button type="button" className="product-secondary" disabled={busy}
                  onClick={() => void action("cancel")}>返回登录</button>
              </div>
            ) : (
              <form onSubmit={submit} aria-busy={disabled}>
                {mode === "recover" ? (
                  <button className="product-recovery-back" type="button" disabled={disabled} onClick={() => changeMode("login")}>
                    返回登录
                  </button>
                ) : <div
                  className="product-tabs"
                  role="group"
                  aria-label="选择登录方式"
                >
                  <button
                    type="button"
                    aria-pressed={mode === "login"}
                    disabled={disabled}
                    onClick={() => changeMode("login")}
                  >
                    登录
                  </button>
                  <button
                    type="button"
                    aria-pressed={mode === "register"}
                    disabled={disabled}
                    onClick={() => changeMode("register")}
                  >
                    注册
                  </button>
                </div>}
                {mode === "register" && (
                  <label className="product-field">
                    昵称
                    <input
                      autoComplete="nickname"
                      placeholder="我们该怎么称呼你？"
                      required
                      maxLength={40}
                      value={fields.displayName}
                      disabled={disabled}
                      onChange={(event) =>
                        edit("displayName", event.target.value)
                      }
                    />
                  </label>
                )}
                <label className="product-field">
                  邮箱
                  <div className="product-email-row">
                    <input
                      type="email"
                      placeholder="你的邮箱地址"
                      autoComplete="username"
                      autoCapitalize="none"
                      spellCheck={false}
                      required
                      maxLength={254}
                      value={fields.email}
                      disabled={disabled}
                      onChange={(event) => edit("email", event.target.value)}
                    />
                    {mode !== "login" && (
                      <button
                        type="button"
                        disabled={disabled || cooldown}
                        onClick={() => void send()}
                      >
                        {sending
                          ? "发送中…"
                          : cooldown
                            ? `${remaining} 秒后重发`
                            : challenge?.mode === mode
                              ? "重新发送"
                              : "发送验证码"}
                      </button>
                    )}
                  </div>
                </label>
                {mode !== "login" && (
                  <label className="product-field">
                    邮箱验证码
                    <input
                      inputMode="numeric"
                      placeholder="输入邮件中的验证码"
                      autoComplete="one-time-code"
                      required
                      pattern={mode === "register" ? "[0-9]{6}" : "[0-9]{6,10}"}
                      maxLength={mode === "register" ? 6 : 10}
                      value={fields.code}
                      disabled={disabled}
                      onChange={(event) =>
                        edit("code", event.target.value.replace(/\D/g, ""))
                      }
                    />
                  </label>
                )}
                <PasswordField
                  key={`password-${mode}`}
                  label={mode === "recover" ? "新密码" : "密码"}
                  current={mode === "login"}
                  value={fields.password}
                  onChange={(value) => edit("password", value)}
                  disabled={disabled}
                />
                {mode !== "login" && (
                  <PasswordField
                    label="确认密码"
                    value={fields.confirm}
                    onChange={(value) => edit("confirm", value)}
                    disabled={disabled}
                    error={mismatch ? "两次输入的密码不一致" : undefined}
                  />
                )}
                {(notice || state.error) && (
                  <p
                    role={failed || state.error ? "alert" : "status"}
                    aria-live="polite"
                    className={
                      failed || state.error ? "product-error" : "product-notice"
                    }
                  >
                    {notice || state.error}
                  </p>
                )}
                <button
                  className="product-primary"
                  type="submit"
                  disabled={disabled}
                >
                  {busy
                    ? "正在确认…"
                    : mode === "register"
                      ? "注册并进入"
                      : mode === "recover"
                        ? "重置密码并进入"
                        : "登录并进入"}
                  <Icon name={busy ? "loading" : "forward"} size={17} />
                </button>
                {mode === "login" && (
                  <button
                    className="product-forgot"
                    type="button"
                    disabled={disabled}
                    onClick={() => changeMode("recover")}
                  >
                    忘记密码？
                  </button>
                )}
              </form>
            )}
            {state.status === "error" && (
              <button
                className="product-secondary"
                disabled={disabled}
                onClick={() => void action("restore")}
              >
                重新连接
              </button>
            )}

            </div>
          </section>
        </main>
      )}
    </AccountContext.Provider>
  );
}

"""Prepare a temporary WeChat *copy* for key capture.

Never codesigns or modifies /Applications/WeChat.app. The copy is ad-hoc signed
with get-task-allow and without Hardened Runtime, used only for one short
capture window, then deleted. SIP stays on; no sudo.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import time
from pathlib import Path

from .mac_capture import ENTITLEMENTS, CaptureError, official_signature_text_ok

BUNDLE_ID = "com.tencent.xinWeChat"
RECORD_NAME = "bootstrap.json"
SHADOW_NAME = "WeChat-trial.app"
DISABLED_NAME = "WeChat-trial.disabled"
KEYS_NAME = "keys.json"
LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


def default_state_dir(home: Path | None = None) -> Path:
    """Noteford Mac layout: data lives under ~/Noteford/Data."""

    if home is None:
        from .product_workspace import process_root
        root = process_root()
        if root is not None:
            return root / 'keycap'
    return (home or Path.home()) / "Noteford" / "Data" / "keycap"


def ensure_state_dir(state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass
    return state_dir


def shadow_app(state_dir: Path) -> Path:
    return state_dir / SHADOW_NAME


def disabled_app(state_dir: Path) -> Path:
    return state_dir / DISABLED_NAME


def is_official_wechat_path(path: Path) -> bool:
    """True for the real /Applications/WeChat.app. Copies elsewhere are not official."""

    try:
        text = str(path.resolve()) if path.exists() else str(path)
    except OSError:
        text = str(path)
    official = "/Applications/WeChat.app"
    return text == official or text.startswith(official + "/")


def iter_app_bundles(root: Path) -> list[Path]:
    """Nested ``.app`` bundles, deepest first, including ``root`` when it is one.

    Launch Services must be unregistered before the tree is deleted. ``lsregister
    -u`` cannot see a path that is already gone, so callers walk this list first.
    """

    bundles: list[Path] = []
    if not root.exists():
        if root.name.endswith(".app"):
            return [root]
        return []
    if root.is_dir() and root.name.endswith(".app"):
        bundles.append(root)
    try:
        for path in root.rglob("*"):
            if path.is_dir() and path.name.endswith(".app") and path != root:
                bundles.append(path)
    except OSError:
        pass
    bundles.sort(key=lambda item: len(item.parts), reverse=True)
    return bundles


def legacy_copy_paths(state_dir: Path, *, home: Path | None = None) -> list[Path]:
    """Historical ad-hoc copies from earlier experiments. Never the official app."""

    root = home or Path.home()
    noteford = root / "Noteford"
    candidates = [
        disabled_app(state_dir),
        noteford / "应用" / "WeChat.app",
        noteford / "应用" / "数据" / "mac-pipeline" / "WeChat-test.app",
        noteford / "应用" / "数据" / "keycap" / DISABLED_NAME,
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen or is_official_wechat_path(path):
            continue
        seen.add(key)
        unique.append(path)
    return unique


def inject_agent_plist(app: Path) -> None:
    """Hide the copy from the Dock without forbidding its login window.

    ``LSUIElement`` keeps the copy out of the Dock and the app switcher, and it
    can still show a window. ``LSBackgroundOnly`` would suppress every window,
    including the QR sheet, so it is never set. Call this before ``sign_copy``:
    editing the plist invalidates the signature, and the copy is ad-hoc signed
    immediately afterwards.
    """

    plist_path = app / "Contents" / "Info.plist"
    if not plist_path.is_file():
        raise CaptureError("wechat_app_missing")
    with plist_path.open("rb") as handle:
        info = plistlib.load(handle)
    if not isinstance(info, dict):
        raise CaptureError("wechat_app_missing")
    info.pop("LSBackgroundOnly", None)
    info["LSUIElement"] = True
    with plist_path.open("wb") as handle:
        plistlib.dump(info, handle)


def copy_is_agent(app: Path) -> bool:
    """True when the copy plist already has LSUIElement and not LSBackgroundOnly."""

    plist_path = app / "Contents" / "Info.plist"
    if not plist_path.is_file():
        return False
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return False
    if not isinstance(info, dict) or info.get("LSBackgroundOnly"):
        return False
    return bool(info.get("LSUIElement"))


def activate_scan_script(pid: int | None = None) -> str:
    """AppleScript that only brings the scan window forward. No clicks."""

    if pid:
        return (
            'tell application "System Events" to set frontmost of '
            f"(first process whose unix id is {int(pid)}) to true"
        )
    return 'tell application id "com.tencent.xinWeChat" to activate'


def keys_path(state_dir: Path) -> Path:
    return state_dir / KEYS_NAME


def read_bundle(app: Path) -> dict:
    plist_path = app / "Contents" / "Info.plist"
    if not plist_path.is_file():
        raise CaptureError("wechat_app_missing")
    with plist_path.open("rb") as handle:
        info = plistlib.load(handle)
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise CaptureError("not_official_wechat")
    return info


def bundle_version(info: dict) -> tuple[str, str]:
    return str(info.get("CFBundleShortVersionString") or ""), str(info.get("CFBundleVersion") or "")


def classify_signature(text: str) -> str:
    if "Signature=adhoc" in text or "flags=0x2(adhoc)" in text:
        return "adhoc"
    if official_signature_text_ok(text):
        return "official"
    if "Apple Mac OS Application Signing" in text or "3rd Party Mac Developer Application" in text:
        return "app_store"
    if text.strip():
        return "other"
    return "unknown"


def load_record(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_record(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def codesign_copy_command(app: Path, entitlements: Path) -> list[str]:
    """Ad-hoc sign the *copy* only — MAIN BUNDLE ONLY, no --deep.

    为什么不用 --deep：微信的反篡改自检在 libwxld.dylib / wechat.dylib 里，
    会用 SecCodeCopySelf / SecCodeCopySigningInformation 检查签名。若 --deep 把
    嵌套组件(尤其 Contents/Frameworks/ld/libwxld.dylib)一并 ad-hoc 重签，就会抹掉
    它们原本的 Tencent Developer ID 签名(TeamIdentifier=5A4RE8SF68)→ 自检发现被
    改签 → exit 255 被秒杀。改为只对主 bundle 签名(不 --deep)后，新 ditto 出来的
    嵌套组件保留原版 Tencent 签名，自检更容易通过；只有主可执行变成 ad-hoc 并带上
    get-task-allow 以便 LLDB 调试。

    Do NOT pass --options runtime (Hardened Runtime blocks debugging).
    """

    return [
        "/usr/bin/codesign",
        "--force",
        "--sign",
        "-",
        "--timestamp=none",
        "--entitlements",
        str(entitlements),
        "--generate-entitlement-der",
        str(app),
    ]


def merged_entitlements_plist(original_xml: str = "") -> bytes:
    """Merge-式重签的 entitlements：保留微信原有 entitlements，并入 get-task-allow。

    对比旧的 replace 式（只写一个 get-task-allow 的最小 plist，会丢掉微信原权限），
    merge 式尽量保留原签名声明，降低 4.1.x 反篡改自检(exit 255)的概率
    （参考 leeguoo wechat-use / wechat-cli.com：只动副本、保留原 entitlement）。

    original_xml 来自 ``codesign -d --entitlements - --xml <副本>``；不同 macOS
    版本导出格式/前缀可能不同（旧版带 0xFADE7171 魔数前缀），这里尽量兼容，
    解析失败则退回"仅 get-task-allow"，与旧行为等价。真机需复核导出格式。
    """

    import plistlib

    data: dict = {}
    text = original_xml or ""
    start = text.find("<?xml")
    if start < 0:
        start = text.find("<plist")
    if start >= 0:
        try:
            data = plistlib.loads(text[start:].encode("utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    # Hardened Runtime 是签名 option(--options runtime)，不是 entitlement；
    # 我们本来就不传 --options runtime，所以这里只需并入调试+注入授权。
    #
    # DYLD interpose 路线关键三件套：
    #  - get-task-allow：允许调试(LLDB fallback 用)。
    #  - cs.allow-dyld-environment-variables：允许进程读取 DYLD_INSERT_LIBRARIES，
    #    否则带 Hardened Runtime 的组件会让内核剥离该环境变量，钩子注入不进去。
    #  - cs.disable-library-validation：微信嵌套组件(libwxld.dylib 等)保留原版
    #    Tencent Developer ID 签名且带 Hardened Runtime，若不关库校验，加载我们
    #    ad-hoc 签名的钩子 dylib 会被拒。关掉后主进程才肯载入异团队/ad-hoc 库。
    data["com.apple.security.get-task-allow"] = True
    data["com.apple.security.cs.allow-dyld-environment-variables"] = True
    data["com.apple.security.cs.disable-library-validation"] = True
    return plistlib.dumps(data)


def _parse_entitlements_xml(xml_text: str):
    """Parse a ``codesign -d --entitlements - --xml`` dump into a dict.

    codesign prints a leading ``Executable=...`` line before the XML and may emit
    other noise, so we locate the plist start. Returns an empty dict on any failure
    (caller decides whether that is fatal).
    """

    import plistlib

    text = xml_text or ""
    start = text.find("<?xml")
    if start < 0:
        start = text.find("<plist")
    if start < 0:
        return {}
    try:
        data = plistlib.loads(text[start:].encode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def assess_pipeline(
    official_app: Path,
    signature_text: str,
    state_dir: Path,
    *,
    keys_present: bool | None = None,
) -> dict:
    """Status for the copy+LLDB pipeline (original stays official forever)."""

    record = load_record(state_dir / RECORD_NAME)
    if keys_present is None:
        from .mac_key_storage import database_payload
        keys_present = bool(database_payload(keys_path(state_dir))["keys"])
    plist_path = official_app / "Contents" / "Info.plist"
    if not plist_path.is_file():
        return _status("missing", "app_missing", "", "", record, keys_present)
    try:
        info = read_bundle(official_app)
    except CaptureError as exc:
        return _status(
            "missing" if exc.code == "wechat_app_missing" else "not_wechat",
            exc.code,
            "",
            "",
            record,
            keys_present,
        )
    version, build = bundle_version(info)
    kind = classify_signature(signature_text)
    if kind == "app_store":
        return _status("blocked", "app_store_wechat", version, build, record, keys_present)
    if kind == "adhoc":
        return _status("blocked", "official_already_adhoc", version, build, record, keys_present)
    if kind != "official":
        return _status("blocked", "not_official_signature", version, build, record, keys_present)
    if keys_present:
        return _status("ready", "keys_cached", version, build, record, keys_present)
    if shadow_app(state_dir).is_dir():
        return _status("needs_capture", "copy_ready", version, build, record, keys_present)
    if disabled_app(state_dir).is_dir() and not keys_present:
        return _status("needs_prepare", "previous_copy_disabled", version, build, record, keys_present)
    return _status("needs_prepare", "first_capture", version, build, record, keys_present)


# Backwards-compatible name used by older imports/tests.
def assess_wechat(app: Path, signature_text: str, record: dict | None) -> dict:
    state_dir = default_state_dir()
    if record is not None:
        # Tests pass a synthetic record; infer keys from schema marker.
        keys_present = bool(record.get("keys_verified"))
    else:
        keys_present = False
    # Use a throwaway state when tests inject records without a real dir.
    return assess_pipeline(app, signature_text, state_dir, keys_present=keys_present)


def _status(
    state: str,
    reason: str,
    version: str,
    build: str,
    record: dict | None,
    keys_present: bool,
) -> dict:
    recorded = str((record or {}).get("version") or "")
    return {
        "state": state,
        "reason": reason,
        "version": version,
        "build": build,
        "recorded_version": recorded,
        "keys_present": keys_present,
        "prompt": _prompt(state, reason, version),
        "scan_required": state == "needs_capture" or (state == "needs_prepare" and reason == "first_capture"),
    }


def _prompt(state: str, reason: str, version: str) -> str:
    if state == "ready":
        return "已有经验证的逐库密钥。日常只读复制/解密即可，不必再取钥。"
    prompts = {
        "app_missing": "没有找到 WeChat.app。",
        "not_official_wechat": "这个包不是 com.tencent.xinWeChat。",
        "app_store_wechat": "这是 App Store 版微信。请改用官网 Developer ID 安装包。",
        "official_already_adhoc": "原版微信已是 ad-hoc 签名。请先从官网重装腾讯 Developer ID 版，再跑副本取钥。",
        "not_official_signature": "原版签名不是腾讯 Developer ID，不会对它做任何操作。",
        "first_capture": f"微信 {version or ''} 仍是官方签名（保持不动）。需要：退出微信 → 复制副本 → LLDB 启动副本 → 扫码登录取钥。",
        "copy_ready": "临时副本已准备好。下一步用 capture 命令启动 LLDB 并扫码登录（早上验收这一步）。",
        "previous_copy_disabled": "上次副本已禁用且尚无密钥。可重新 prepare 后再 capture。",
        "keys_cached": "密钥已缓存。",
    }
    return prompts.get(reason, "需要准备副本并取钥。")


def restore_steps() -> list[str]:
    return [
        "本方案不改 /Applications/WeChat.app 的签名。若原版异常，从微信官网重装即可。",
        "临时副本取完已自动删除，并已注销 Launch Services，无需手动删除 WeChat-trial.app 或 .disabled。",
        "用精确路径启动原版：/Applications/WeChat.app/Contents/MacOS/WeChat",
        "保持系统完整性保护开启；整个过程不使用 sudo。",
        "密钥只保存在系统钥匙串；旧密钥文件经回读验证后迁移清除。",
    ]


def prepare_copy(
    official_app: Path,
    state_dir: Path,
    actions,
    *,
    signature_text: str,
    force: bool = False,
) -> dict:
    """Quit WeChat, ditto a private copy, ad-hoc sign the copy only."""

    ensure_state_dir(state_dir)
    status = assess_pipeline(official_app, signature_text, state_dir)
    if status["state"] == "blocked":
        raise CaptureError(status["reason"])
    if status["state"] == "missing" or status["state"] == "not_wechat":
        raise CaptureError(status["reason"])
    if status["state"] == "ready" and not force:
        status["changed"] = False
        return status
    shadow = shadow_app(state_dir)
    disabled = disabled_app(state_dir)
    if shadow.exists() and not force:
        ready = assess_pipeline(official_app, signature_text, state_dir)
        ready["changed"] = False
        ready["shadow"] = str(shadow)
        return ready
    if status.get("keys_present") and not force:
        raise CaptureError("keys_exist_use_new_state_or_force")
    if disabled.exists() and force:
        actions.remove_tree(disabled)
    if shadow.exists() and force:
        actions.remove_tree(shadow)

    info = read_bundle(official_app)
    version, build = bundle_version(info)
    actions.require_wechat_quit()
    baseline = signature_text or actions.signature_text(official_app)
    (state_dir / "original-signature.txt").write_text(baseline, encoding="utf-8")
    try:
        os.chmod(state_dir / "original-signature.txt", 0o600)
    except OSError:
        pass

    entitlements = state_dir / "debug.entitlements"

    actions.copy_app(official_app, shadow)
    actions.strip_attributes(shadow)
    # Dock 隐藏必须发生在重签之前：改 Info.plist 会使签名失效，后面的 ad-hoc 重签会盖上。
    # 只用 LSUIElement，不用 LSBackgroundOnly（后者会把扫码窗也禁掉）。
    inject_agent_plist(shadow)
    # merge-式重签：先导出副本(=原版)的原 entitlements，并入 get-task-allow 后再签，
    # 保留微信原签名声明(app-sandbox / application-groups / keychain / com.apple.*)，
    # 降低反篡改自检(exit 255)概率。
    #
    # 关键：当运行在真机(actions 暴露 original_entitlements)时，导出失败必须**显式报错
    # 中止**，绝不静默退回"仅 get-task-allow"的最小授权 —— 那正是上一版踩的坑(悄悄回退
    # 导致副本被秒杀)。只有在没有导出能力的环境(如单测的最小 actions)才允许最小授权。
    getter = getattr(actions, "original_entitlements", None)
    if callable(getter):
        original_ent = getter(shadow)  # 失败会抛 CaptureError，向上传播、不吞
        parsed = _parse_entitlements_xml(original_ent)
        if not parsed:
            # 导出返回了空/无法解析的内容：中止，不回退最小授权。
            raise CaptureError("entitlements_export_failed")
        merged = merged_entitlements_plist(original_ent)
        # 自检：合并后必须既含 get-task-allow，又保留了原版至少一条 entitlement。
        import plistlib as _pl
        merged_dict = _pl.loads(merged)
        if not merged_dict.get("com.apple.security.get-task-allow"):
            raise CaptureError("entitlements_merge_failed")
        if not any(k != "com.apple.security.get-task-allow" for k in merged_dict):
            raise CaptureError("entitlements_merge_degenerated_to_minimal")
        entitlements.write_bytes(merged)
    else:
        # 无导出能力的环境：仅 get-task-allow(与旧行为等价，仅用于测试/降级)。
        entitlements.write_bytes(merged_entitlements_plist(""))
    try:
        os.chmod(entitlements, 0o600)
    except OSError:
        pass
    actions.sign_copy(shadow, entitlements)
    after = actions.signature_text(official_app)
    if after != baseline and baseline.strip():
        # Soft check: exact string match can fail on nondeterministic fields; verify official still ok.
        if classify_signature(after) != "official":
            raise CaptureError("original_signature_changed")

    save_record(
        state_dir / RECORD_NAME,
        {
            "schema": 2,
            "method": "copy_lldb_commoncrypto",
            "bundle_id": BUNDLE_ID,
            "version": version,
            "build": build,
            "official_app": str(official_app),
            "shadow": str(shadow),
        },
    )
    result = assess_pipeline(official_app, baseline, state_dir)
    result["changed"] = True
    result["shadow"] = str(shadow)
    result["command"] = codesign_copy_command(shadow, entitlements)
    return result


def cleanup_legacy_copies(state_dir: Path, actions, *, home: Path | None = None) -> list[str]:
    """Unregister and delete leftover ad-hoc copies. Idempotent and non-fatal.

    Skips ``/Applications/WeChat.app``. A missing path is a no-op. One leftover
    that fails to delete does not stop the others.
    """

    removed: list[str] = []
    for path in legacy_copy_paths(state_dir, home=home):
        if is_official_wechat_path(path) or not path.exists():
            continue
        try:
            unregister = getattr(actions, "unregister_launch_services", None)
            if callable(unregister):
                unregister(path)
            actions.remove_tree(path)
        except Exception:
            continue
        if not path.exists():
            removed.append(str(path))
    return removed


def restore_original(
    official_app: Path,
    state_dir: Path,
    actions,
    *,
    relaunch: bool = True,
    home: Path | None = None,
) -> dict:
    """Delete the trial copy and launch the official binary by exact path.

    The copy is unregistered (including nested ``.app`` bundles) and then removed.
    Historical ``.disabled`` leftovers are removed the same way. The official app
    is never deleted or signed.
    """

    if is_official_wechat_path(state_dir) or is_official_wechat_path(shadow_app(state_dir)):
        raise CaptureError("refusing_to_delete_official_app")
    shadow = shadow_app(state_dir)
    stop = getattr(actions, "stop_shadow_processes", None)
    if callable(stop):
        stop(shadow)
    if actions.shadow_still_running(shadow):
        raise CaptureError("quit_trial_copy_first")
    if shadow.exists():
        actions.unregister_launch_services(shadow)
        actions.remove_tree(shadow)
    cleanup_legacy_copies(state_dir, actions, home=home)
    if relaunch and not actions.wechat_running():
        exe = official_app / "Contents" / "MacOS" / "WeChat"
        actions.launch_exact(exe)
    baseline_path = state_dir / "original-signature.txt"
    current = actions.signature_text(official_app)
    unchanged = True
    if baseline_path.is_file():
        unchanged = current == baseline_path.read_text(encoding="utf-8") or classify_signature(current) == "official"
    return {
        "ok": True,
        "trial_removed": not shadow.exists(),
        "trial_disabled": False,
        "original_signature_ok": unchanged and classify_signature(current) == "official",
        "sip_note": "csrutil status should remain enabled",
    }


# Deprecated alias — callers must migrate to prepare_copy.
def resign_official(*_args, **_kwargs):
    raise CaptureError("resign_official_abandoned_use_prepare_copy")


class DarwinResignActions:
    """File and process helpers for a real Mac. No sudo. Never signs the official app."""

    def signature_text(self, app: Path) -> str:
        completed = subprocess.run(
            ["/usr/bin/codesign", "-dv", "--verbose=4", str(app)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return completed.stdout + completed.stderr

    def original_entitlements(self, app: Path) -> str:
        """Export the app's current entitlements as XML for merge-式重签.

        macOS 13+/26 都支持 ``codesign -d --entitlements - --xml``(stdout 带一行
        ``Executable=...`` 前缀再接 XML)。导出失败必须抛错让 prepare_copy 中止，
        **不再**静默返回空字符串触发最小授权回退(那正是上次被秒杀的根因)。
        """

        completed = subprocess.run(
            ["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml", str(app)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (completed.stdout or "")
        if completed.returncode != 0 or ("<plist" not in out and "<?xml" not in out):
            # 某些旧系统用 ``:-`` 变体；再试一次作为兜底。
            alt = subprocess.run(
                ["/usr/bin/codesign", "-d", "--entitlements", ":-", str(app)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            out = alt.stdout or ""
            if alt.returncode != 0 or ("<plist" not in out and "<?xml" not in out):
                raise CaptureError("entitlements_export_failed")
        return out

    def require_wechat_quit(self) -> None:
        if _process_exists("WeChat") or _process_exists("WeChatAppEx"):
            # Prefer asking the user to quit via menu; we only attempt a gentle quit once.
            subprocess.run(
                ["/usr/bin/osascript", "-e", 'tell application "WeChat" to quit'],
                check=False,
                timeout=30,
            )
            if not _wait_until_quit(25):
                raise CaptureError("quit_wechat_normally_first")

    def quit_wechat(self) -> None:
        self.require_wechat_quit()

    def copy_app(self, source: Path, dest: Path) -> None:
        if dest.exists():
            raise CaptureError("shadow_exists")
        completed = subprocess.run(
            ["/usr/bin/ditto", str(source), str(dest)],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if completed.returncode != 0 or not dest.is_dir():
            raise CaptureError("copy_failed")

    def strip_attributes(self, app: Path) -> None:
        # 先给整个副本补上写权限：有些缓存文件(如 gpu_shader_cache.bin)权限受限，
        # 直接 xattr -cr 会 "Permission denied"。副本是我们自己的拷贝，chmod 安全。
        subprocess.run(["/bin/chmod", "-R", "u+w", str(app)], check=False, timeout=120)
        # 清掉全部扩展属性(含 com.apple.quarantine)；再专门删一次 quarantine 兜底。
        subprocess.run(["/usr/bin/xattr", "-cr", str(app)], check=False, timeout=120)
        subprocess.run(
            ["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(app)],
            check=False,
            timeout=120,
        )

    def sign_copy(self, app: Path, entitlements: Path) -> None:
        # Only the copy. Official path must never appear here.
        if str(app).startswith("/Applications/"):
            raise CaptureError("refusing_to_sign_applications")
        try:
            completed = subprocess.run(
                codesign_copy_command(app, entitlements),
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise CaptureError("codesign_failed") from exc
        if completed.returncode != 0:
            raise CaptureError("codesign_failed")
        # 只验证我们刚签的主 bundle 签名有效即可(不 --deep --strict)。
        # 微信原包里有松散资源(如 XPlayer.app/.../vk_swiftshader_icd.json)会让
        # --deep --strict 误报 "not signed at all"；那是原包固有结构、与副本能否运行无关，
        # 不应作为中止条件。主签名有效 + get-task-allow 到位才是关键。
        verify = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--verbose=2", str(app)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if verify.returncode != 0:
            raise CaptureError("codesign_verify_failed")

    def sign(self, app: Path, entitlements: Path) -> None:
        self.sign_copy(app, entitlements)

    def open_wechat(self, app: Path) -> None:
        subprocess.run(["/usr/bin/open", str(app)], check=False, timeout=20)

    def launch_exact(self, exe: Path) -> None:
        # Launch Services gives the official app its own normal launch context,
        # instead of running it as a child of Noteford's service (TCC attribution).
        app = exe.parent.parent.parent
        completed = subprocess.run(
            ["/usr/bin/open", "-a", str(app)],
            capture_output=True, timeout=20, check=False,
        )
        if completed.returncode:
            raise CaptureError("official_launch_failed")

    def wechat_running(self) -> bool:
        return _process_exists("WeChat") or _process_exists("WeChatAppEx")

    def _shadow_processes(self, shadow: Path):
        import psutil
        if is_official_wechat_path(shadow):
            raise CaptureError("refusing_to_delete_official_app")
        prefix = str(shadow.absolute()) + "/"
        found = []
        for proc in psutil.process_iter(["exe"]):
            try:
                if str(proc.info.get("exe") or "").startswith(prefix):
                    found.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return found

    def stop_shadow_processes(self, shadow: Path) -> None:
        import psutil
        processes = self._shadow_processes(shadow)
        for proc in processes:
            try:
                proc.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(processes, timeout=5)
        for proc in alive:
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=5)
        if alive:
            raise CaptureError("quit_trial_copy_first")

    def shadow_still_running(self, shadow: Path) -> bool:
        return bool(self._shadow_processes(shadow))

    def unregister_launch_services(self, app: Path) -> None:
        """``lsregister -u`` every nested ``.app`` before the tree is deleted.

        Non-fatal: a missing ``lsregister`` or one failing child does not raise.
        Must run while the bundles still exist on disk.
        """

        if is_official_wechat_path(app):
            return
        if not Path(LSREGISTER).is_file():
            return
        for bundle in iter_app_bundles(app):
            if is_official_wechat_path(bundle):
                continue
            try:
                subprocess.run(
                    [LSREGISTER, "-u", str(bundle)],
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue

    def nudge_scan_window(self, exe: Path, pid: int | None = None) -> None:
        """Bring an agent-style copy forward so the QR can be scanned.

        LSUIElement apps often do not activate themselves. This is a single
        AppleScript activate, not a click. Failure is ignored.
        """

        del exe  # the script targets the pid or the WeChat bundle id
        script = activate_scan_script(pid)
        try:
            subprocess.Popen(
                ["/usr/bin/osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return

    def rename(self, source: Path, dest: Path) -> None:
        source.rename(dest)

    def remove_tree(self, path: Path) -> None:
        if is_official_wechat_path(path):
            raise CaptureError("refusing_to_delete_official_app")
        subprocess.run(["/bin/rm", "-rf", str(path)], check=False, timeout=120)


def _process_exists(name: str) -> bool:
    completed = subprocess.run(["/usr/bin/pgrep", "-x", name], check=False, capture_output=True, timeout=10)
    return completed.returncode == 0


def _wait_until_quit(seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _process_exists("WeChat") and not _process_exists("WeChatAppEx"):
            return True
        time.sleep(0.3)
    return False

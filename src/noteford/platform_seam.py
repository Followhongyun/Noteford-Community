"""Platform seams for the macOS port.

Importing this module does not import ``winmem`` or the macOS capture modules.
Windows memory scan and macOS interpose capture load only when a key call
needs them. Shared refresh, discovery, and selective code ask
``acquire_database_key_candidates`` which host to use. The Windows scan itself
stays in ``winmem``; calling it on another platform raises
``PlatformCapabilityError`` instead of failing during import.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Protocol


SERVICE_EXECUTABLE_NAMES = {
    "win32": "noteford-service.exe",
    "darwin": "noteford-service",
}

WECHAT_PROCESS_NAMES = {
    "win32": ("Weixin.exe",),
    "darwin": ("WeChat", "WeChatAppEx"),
}

# SQLCipher 4 passphrase KDF. Raw-key mode (crypto.verify_key) does not use it.
# A macOS 4.1+ import may supply the passphrase instead of the derived raw key.
SQLCIPHER4_KDF_ITERATIONS = 256000
SQLCIPHER4_KEY_LENGTH = 32
SQLCIPHER4_SALT_LENGTH = 16


def service_executable_name(platform: str | None = None) -> str:
    platform = platform or sys.platform
    try:
        return SERVICE_EXECUTABLE_NAMES[platform]
    except KeyError:
        raise NotImplementedError(f"no service executable name for {platform}") from None


def wechat_process_names(platform: str | None = None) -> tuple[str, ...]:
    platform = platform or sys.platform
    try:
        return WECHAT_PROCESS_NAMES[platform]
    except KeyError:
        raise NotImplementedError(f"no WeChat process names for {platform}") from None


def writer_lock_backend(platform: str | None = None) -> str:
    """Name of the lock already implemented in runtime_lock.py / local_pairing.py."""

    platform = platform or sys.platform
    if platform == "win32":
        return "msvcrt"
    if platform == "darwin":
        return "fcntl"
    raise NotImplementedError(f"no writer lock backend for {platform}")


def secret_backend(platform: str | None = None) -> str:
    platform = platform or sys.platform
    if platform == "win32":
        return "dpapi"
    if platform == "darwin":
        return "keychain"
    raise NotImplementedError(f"no secret backend for {platform}")


def default_data_root(
    platform: str | None = None,
    *,
    home: Path | None = None,
    local_app_data: Path | None = None,
) -> Path:
    """Default packaged data root.

    Electron still decides the live root, including the Windows portable layout.
    This helper is only the platform default used when no portable marker exists.
    """

    platform = platform or sys.platform
    home = home or Path.home()
    if platform == "win32":
        base = local_app_data or (home / "AppData" / "Local")
        return base / "Noteford"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Noteford"
    raise NotImplementedError(f"no data root for {platform}")


def wechat_search_roots(platform: str | None = None, *, home: Path | None = None) -> tuple[Path, ...]:
    """Static directories a future discovery pass should inspect.

    Windows also reads ``%APPDATA%\\Tencent\\xwechat\\config\\*.ini``. That scan
    stays in discovery.py until a Mac config file is confirmed on device.
    """

    platform = platform or sys.platform
    home = home or Path.home()
    if platform == "win32":
        return (
            home / "Documents" / "WeChat Files",
            home / "Documents" / "Tencent Files",
            home / "Documents",
            home,
        )
    if platform == "darwin":
        container = home / "Library" / "Containers" / "com.tencent.xinWeChat" / "Data"
        return (
            container / "Documents" / "app_data" / "xwechat_files",
            container / "Documents" / "xwechat_files",
            home / "Documents" / "xwechat_files",
            container / "Library" / "Application Support" / "com.tencent.xinWeChat",
        )
    raise NotImplementedError(f"no WeChat search roots for {platform}")


def derive_sqlcipher4_raw_key(passphrase: bytes, salt: bytes) -> bytes:
    """Derive the 32-byte AES key SQLCipher 4 uses for a passphrase.

    The on-disk page format stays the one ``crypto.verify_key`` already checks.
    Iteration count 256000 applies only to this derivation step.
    """

    if not passphrase:
        raise ValueError("passphrase is empty")
    if len(salt) != SQLCIPHER4_SALT_LENGTH:
        raise ValueError("salt must be 16 bytes")
    return hashlib.pbkdf2_hmac(
        "sha512",
        passphrase,
        salt,
        SQLCIPHER4_KDF_ITERATIONS,
        dklen=SQLCIPHER4_KEY_LENGTH,
    )


def normalize_key_candidate(item: dict) -> dict:
    """Accept one user-imported candidate in the candidate_keys.json shape."""

    key = bytes.fromhex(str(item["key_hex"]))
    if len(key) != SQLCIPHER4_KEY_LENGTH:
        raise ValueError("key must be 32 bytes")
    salt_hex = item.get("salt_hex")
    if salt_hex is not None:
        salt = bytes.fromhex(str(salt_hex))
        if len(salt) != SQLCIPHER4_SALT_LENGTH:
            raise ValueError("salt must be 16 bytes")
        salt_hex = salt.hex()
    return {"key_hex": key.hex(), "salt_hex": salt_hex}


class DatabaseKeySource(Protocol):
    def candidate_keys(self) -> list[dict]:
        """Return ``{key_hex, salt_hex}`` records for ``crypto.find_matching_key``."""


class ImportedKeySource:
    """Manual key file kept for tests. macOS uses ``macos_database_key_source``."""

    def __init__(self, candidates: list[dict]):
        self._candidates = [normalize_key_candidate(item) for item in candidates]

    def candidate_keys(self) -> list[dict]:
        return list(self._candidates)


class PlatformCapabilityError(RuntimeError):
    """A platform-specific key or memory path was used on the wrong host."""


class WindowsMemoryKeySource:
    """Read-only ``Weixin.exe`` Config.Cipher scan.

    ``candidate_keys`` imports ``winmem`` only on Windows, and only when called.
    """

    def candidate_keys(self) -> list[dict]:
        if sys.platform != "win32":
            raise PlatformCapabilityError(
                "Windows memory scan is only available on Windows. "
                "On macOS use macos_database_key_source (Keychain / interpose); "
                "memory scanning is refused."
            )
        from .winmem import scan_config_cipher_candidates

        return [
            {
                "key_hex": item.key_hex,
                "salt_hex": item.salt_hex,
                "fingerprint": item.fingerprint,
            }
            for item in scan_config_cipher_candidates()
        ]


class MacMemoryKeySource:
    def candidate_keys(self) -> list[dict]:
        raise NotImplementedError(
            "macOS does not scan WeChat memory. Use macos_database_key_source"
        )


def list_host_wechat_processes(platform: str | None = None) -> list[dict]:
    """Windows process list used by diagnostics.

    macOS does not enumerate ``Weixin.exe``. Key capture stays on the interpose
    pipeline; this function refuses that host instead of scanning memory.
    """

    platform = platform or sys.platform
    if platform == "win32":
        from .winmem import list_weixin_processes

        return list_weixin_processes()
    if platform == "darwin":
        raise PlatformCapabilityError(
            "macOS does not enumerate Weixin.exe. Process names are WeChat and "
            "WeChatAppEx; database keys come from macos_database_key_source."
        )
    raise PlatformCapabilityError(
        f"no WeChat process listing for {platform}. "
        "Windows lists Weixin.exe; macOS uses macos_database_key_source."
    )


def acquire_database_key_candidates(
    platform: str | None = None,
    *,
    keys_file: Path | None = None,
    config=None,
    actions=None,
    store=None,
    socket_path=None,
    capture_file=None,
    route: str = "interpose",
    capture: bool = False,
):
    """Dispatch database-key acquisition.

    Windows returns ``winmem.Candidate`` objects from the existing memory scan.
    The scan function, XOR mask, and saved file shape are unchanged.

    macOS never scans process memory. With ``capture=False`` it reads an existing
    the Keychain (migrating legacy files once) from the interpose pipeline. With ``capture=True`` it calls
    ``macos_database_key_source`` and returns that source's candidate records.
    """

    platform = platform or sys.platform
    if platform == "win32":
        if capture or config is not None:
            raise PlatformCapabilityError(
                "macOS interpose capture does not run on Windows; "
                "the Windows key path remains the read-only memory scan"
            )
        from .winmem import scan_config_cipher_candidates

        return scan_config_cipher_candidates()
    if platform == "darwin":
        return _macos_database_key_candidates(
            keys_file=keys_file,
            config=config,
            actions=actions,
            store=store,
            socket_path=socket_path,
            capture_file=capture_file,
            route=route,
            capture=capture,
        )
    raise PlatformCapabilityError(
        f"no database key source for {platform}. "
        "Windows uses the winmem memory scan; macOS uses Keychain / macos_database_key_source."
    )


def _macos_database_key_candidates(
    *,
    keys_file: Path | None,
    config,
    actions,
    store,
    socket_path,
    capture_file,
    route: str,
    capture: bool,
) -> list[dict]:
    if capture:
        if config is None:
            raise PlatformCapabilityError(
                "macOS key capture requires a CaptureConfig passed to macos_database_key_source"
            )
        source = macos_database_key_source(
            config,
            actions=actions,
            store=store,
            socket_path=socket_path,
            capture_file=capture_file,
            route=route,
        )
        return source.candidate_keys()

    if keys_file is not None:
        path = Path(keys_file)
    else:
        from .mac_resign import default_state_dir, keys_path

        path = keys_path(default_state_dir())
    from .mac_key_storage import database_payload

    loaded = database_payload(path, store)["keys"]
    records = [
        item
        for item in loaded
        if isinstance(item, dict) and isinstance(item.get("key_hex"), str) and item.get("key_hex")
    ]
    if records:
        return records
    raise PlatformCapabilityError(
        "macOS does not scan WeChat memory. "
        f"No keys were found in {path.name}. "
        "Use macos_database_key_source (interpose) to capture keys."
    )


def macos_database_key_source(config, actions=None, store=None, socket_path=None, capture_file=None, route="interpose"):
    """Default macOS key source: DYLD interpose injection into a WeChat copy.

    route="interpose" (default) uses the hook-dylib route (钩子开机即在位，稳)；
    route="lldb" keeps the old LLDB-launch route as fallback.
    See docs/MACOS-INTERPOSE.md.
    """

    if route == "lldb":
        from .mac_capture import MacCopyLldbKeySource

        return MacCopyLldbKeySource(config, actions=actions, store=store)
    from .mac_interpose import MacCopyInterposeKeySource

    return MacCopyInterposeKeySource(config, actions=actions, store=store)


def macos_resign_status(app, signature_text: str, record: dict | None = None, state_dir=None) -> dict:
    from pathlib import Path

    from .mac_resign import assess_pipeline, default_state_dir

    return assess_pipeline(app, signature_text, Path(state_dir) if state_dir else default_state_dir())


def macos_archive_sync(db_root, project_root, store, **kwargs):
    """Read-only database copy, WAL merge, and HMAC recapture flag."""

    from .mac_sync import sync_databases

    return sync_databases(db_root, project_root, store, **kwargs)


class SecretProtector(Protocol):
    def wrap(self, plaintext: bytes) -> bytes: ...

    def unwrap(self, blob: bytes) -> bytes: ...


class MacKeychainSecretProtector:
    """One login-keychain item. The data directory keeps a marker, not key bytes.

    Service ``com.followhongyun.noteford``, account ``noteford-data-key``,
    accessibility ``kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly``.
    Pass ``runner`` only from tests. Production uses the Security framework and
    never retries denied access through another process identity.
    """

    service = "com.followhongyun.noteford"
    account = "noteford-data-key"
    accessible = "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly"

    def __init__(self, runner=None, *, allow_interaction=True, account=None):
        if account is not None:
            self.account = account
        self._runner = runner
        self.allow_interaction = allow_interaction

    def wrap(self, plaintext: bytes) -> bytes:
        from . import keychain_secret

        secret = bytes(plaintext)
        if len(secret) != 32:
            raise ValueError("invalid remote MCP key")
        self._put(secret)
        return keychain_secret.marker(self.service, self.account)

    def unwrap(self, blob: bytes) -> bytes:
        from . import keychain_secret

        keychain_secret.accept_marker(bytes(blob), self.service, self.account)
        secret = self._get()
        if len(secret) != 32:
            raise ValueError("invalid remote MCP key")
        return secret

    def _put(self, secret: bytes) -> None:
        from . import keychain_secret

        if self._runner is not None:
            keychain_secret.cli_put(self.service, self.account, secret, self._runner)
            return
        if sys.platform != "darwin":
            raise OSError("macOS Keychain is unavailable")
        keychain_secret.native_put(self.service, self.account, secret, allow_ui=self.allow_interaction)

    def _get(self) -> bytes:
        from . import keychain_secret

        if self._runner is not None:
            return keychain_secret.cli_get(self.service, self.account, self._runner)
        if sys.platform != "darwin":
            raise OSError("macOS Keychain is unavailable")
        return keychain_secret.native_get(self.service, self.account, allow_ui=self.allow_interaction)


def protected_data_key(key_file: Path, *, platform: str | None = None, protector: MacKeychainSecretProtector | None = None) -> bytes:
    """32-byte store key. Windows stays on DPAPI. macOS uses the keychain marker."""

    path = Path(key_file)
    if secret_backend(platform) == "dpapi":
        from . import remote_mcp

        return remote_mcp.dpapi_key_provider(path)
    import hashlib
    from .keychain_secret import marker
    legacy = marker(MacKeychainSecretProtector.service, 'noteford-data-key')
    account = ('noteford-data-key' if path.is_file() and path.read_bytes() == legacy
               else 'noteford-store-' + hashlib.sha256(str(path.resolve()).encode()).hexdigest())
    if protector is None:
        protector = MacKeychainSecretProtector(account=account)
    else:
        protector.account = account
    if path.is_file():
        key = protector.unwrap(path.read_bytes())
    else:
        if path.with_suffix(".store").is_file():
            raise RuntimeError("pairing store exists but the Keychain marker is missing")
        import secrets

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = AESGCM.generate_key(bit_length=256)
        blob = protector.wrap(key)
        if key in blob or key.hex().encode("ascii") in blob:
            raise RuntimeError("refusing to store the data key on disk")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
        temporary.write_bytes(blob)
        os.replace(temporary, path)
    if len(key) != 32 or (path.is_file() and (key in path.read_bytes() or key.hex().encode("ascii") in path.read_bytes())):
        raise ValueError("invalid remote MCP key")
    return key


def library_source_kind(platform: str | None = None) -> str:
    """Where onboarding looks for accounts.

    Windows (and every non-macOS host, including the Linux test runner) keeps
    the encrypted WeChat file scan. macOS reads the already-decrypted archive.
    """

    platform = platform or sys.platform
    if platform == "darwin":
        return "decrypted_archive"
    return "wechat_files"


def macos_noteford_data_root(home: Path | None = None) -> Path:
    """Mac data tree already used by key capture: ``~/Noteford/Data``."""

    if home is None:
        from .product_workspace import process_root
        root = process_root()
        if root is not None:
            return root
    return (home or Path.home()) / "Noteford" / "Data"


def decrypted_archive_root(platform: str | None = None, *, home: Path | None = None) -> Path | None:
    if library_source_kind(platform) != "decrypted_archive":
        return None
    return macos_noteford_data_root(home) / "data" / "decrypted"


def mcp_discovery_directory(
    platform: str | None = None,
    *,
    home: Path | None = None,
    local_app_data: Path | None = None,
) -> Path:
    """Directory that holds ``connection.json`` for external AI clients.

    Windows stays ``%LOCALAPPDATA%\\Noteford``. macOS is
    ``~/Library/Application Support/Noteford``.
    """

    platform = platform or sys.platform
    if platform == "darwin":
        return (home or Path.home()) / "Library" / "Application Support" / "Noteford"
    base = local_app_data or Path(
        os.environ.get("LOCALAPPDATA", str((home or Path.home()) / "AppData" / "Local"))
    )
    return Path(base) / "Noteford"


def mcp_discovery_file(
    platform: str | None = None,
    *,
    home: Path | None = None,
    local_app_data: Path | None = None,
) -> Path:
    return mcp_discovery_directory(platform, home=home, local_app_data=local_app_data) / "connection.json"


def client_script_name(platform: str | None = None) -> str:
    platform = platform or sys.platform
    if platform == "darwin":
        return "noteford.sh"
    return "noteford.ps1"


_PYTHON_NAMES = {
    "python",
    "python.exe",
    "python3",
    "python3.exe",
}


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _python_launcher(command: Path) -> bool:
    name = command.name.lower()
    if name in _PYTHON_NAMES:
        return True
    return name.startswith("python3.") or name.startswith("python.")


def _module_args(args: list | None) -> bool:
    if not isinstance(args, list) or len(args) < 2:
        return False
    return [str(args[0]), str(args[1])] == ["-m", "noteford.service"]


def service_command_acceptable(
    app_executable: Path,
    command: Path,
    *,
    platform: str | None = None,
    args: list | None = None,
) -> bool:
    """Whether ``connection.json`` points at this app's local service.

    Windows packaged apps must still point at ``resources/service/noteford-service.exe``.
    Dev and macOS may point at ``python -m noteford.service``. A macOS frozen
    binary is ``Contents/Resources/service/noteford-service``.
    """

    platform = platform or sys.platform
    app_executable = Path(app_executable)
    command = Path(command)
    windows_expected = app_executable.parent / "resources" / "service" / "noteford-service.exe"
    mac_expected = (
        app_executable.parent / "resources" / "service" / "noteford-service",
        app_executable.parent.parent / "Resources" / "service" / "noteford-service",
    )
    python_module = _python_launcher(command) and _module_args(args)
    if platform == "win32":
        return _same_file(command, windows_expected) or python_module
    if platform == "darwin":
        return any(_same_file(command, item) for item in mac_expected) or python_module
    return (
        _same_file(command, windows_expected)
        or any(_same_file(command, item) for item in mac_expected)
        or python_module
    )


def discover_library_candidates(
    *,
    wechat_root: Path | None = None,
    configured: list | tuple = (),
    platform: str | None = None,
    home: Path | None = None,
) -> list[dict]:
    """Account rows for onboarding. Windows keeps the existing scan."""

    if library_source_kind(platform) != "decrypted_archive":
        from .discovery import candidate_roots, scan_library_candidates

        roots = [Path(wechat_root)] if wechat_root else candidate_roots(configured)
        return scan_library_candidates(roots)

    from .decrypted_archive import scan_decrypted_archives

    if wechat_root:
        roots = [Path(wechat_root)]
    else:
        archive = decrypted_archive_root(platform, home=home)
        roots = [archive] if archive is not None else []
    from .product_workspace import process_root, source_allowed
    managed = process_root()
    if managed is not None:
        roots = [root for root in roots if source_allowed(managed, root)]
    return scan_decrypted_archives(roots)


def register_discovered_library(registry, candidate: dict, platform: str | None = None):
    root = Path(candidate["source_root"])
    account = str(candidate["account"])
    if (
        library_source_kind(platform) == "decrypted_archive"
        or candidate.get("source_kind") == "decrypted_archive"
    ):
        return registry.register_archive_account(root, account)
    return registry.register_account(root, account)


def refresh_host_library(*, platform: str | None = None, **kwargs):
    """Windows catalog refresh, or the macOS plaintext archive index."""

    if library_source_kind(platform) == "decrypted_archive":
        from .decrypted_archive import refresh_decrypted_library
        from .mac_live_sync import refresh_managed_archive

        live = refresh_managed_archive(kwargs["wechat_root"], kwargs["account"])
        result = refresh_decrypted_library(**kwargs)
        if live is not None:
            result["live_source"] = live
        return result
    from .selective import refresh_catalog

    return refresh_catalog(**kwargs)

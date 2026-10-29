"""Login-keychain item for the Noteford data key.

The disk marker names the item. It does not contain key bytes. Windows DPAPI
stays in ``remote_mcp.dpapi_key_provider`` and is not used here.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from contextlib import contextmanager

ACCESSIBLE = "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly"
MARKER_PREFIX = b"noteford-keychain-v1\n"
_interaction_lock = threading.RLock()


@contextmanager
def _user_interaction(security, allowed):
    """Serialize the legacy login-keychain process-wide UI flag and restore it."""
    import ctypes
    with _interaction_lock:
        security.SecKeychainGetUserInteractionAllowed.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        security.SecKeychainGetUserInteractionAllowed.restype = ctypes.c_int32
        security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_ubyte]
        security.SecKeychainSetUserInteractionAllowed.restype = ctypes.c_int32
        previous = ctypes.c_ubyte()
        if security.SecKeychainGetUserInteractionAllowed(ctypes.byref(previous)) != 0:
            raise OSError('keychain_failed')
        if security.SecKeychainSetUserInteractionAllowed(bool(allowed)) != 0:
            raise OSError('keychain_failed')
        try:
            yield
        finally:
            security.SecKeychainSetUserInteractionAllowed(previous.value)


def marker(service: str, account: str) -> bytes:
    return (
        MARKER_PREFIX
        + service.encode("utf-8")
        + b"\n"
        + account.encode("utf-8")
        + b"\n"
        + ACCESSIBLE.encode("utf-8")
        + b"\n"
    )


def accept_marker(blob: bytes, service: str, account: str) -> None:
    if blob != marker(service, account):
        raise ValueError("invalid keychain marker")


def add_command(service: str, account: str, secret: bytes) -> list[str]:
    return [
        "/usr/bin/security",
        "add-generic-password",
        "-U",
        "-s",
        service,
        "-a",
        account,
        "-w",
        secret.hex(),
    ]


def find_command(service: str, account: str) -> list[str]:
    return [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        service,
        "-a",
        account,
        "-w",
    ]


def cli_put(service: str, account: str, secret: bytes, runner) -> None:
    completed = runner(add_command(service, account, secret), check=False, capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        raise OSError("keychain_failed")


def cli_get(service: str, account: str, runner) -> bytes:
    completed = runner(find_command(service, account), check=False, capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        raise OSError("keychain_failed")
    text = completed.stdout[:-1] if completed.stdout.endswith("\n") else completed.stdout
    try:
        secret = bytes.fromhex(text)
    except ValueError as exc:
        raise OSError("keychain_failed") from exc
    if len(secret) != 32:
        raise OSError("keychain_failed")
    return secret


def native_put(service: str, account: str, secret: bytes, *, allow_ui=True) -> None:
    _native("add", service, account, secret, allow_ui=allow_ui)


def native_get(service: str, account: str, *, allow_ui=True) -> bytes:
    found = _native("get", service, account, None, allow_ui=allow_ui)
    if not isinstance(found, bytes) or len(found) != 32:
        raise OSError("keychain_failed")
    return found


def put_blob(service: str, account: str, payload: bytes) -> None:
    """Update an arbitrary secret in place; never delete the previous item first."""
    _native('put_blob', service, account, payload, allow_ui=True)


def get_blob(service: str, account: str) -> bytes | None:
    """Only errSecItemNotFound means absent; denied/locked reads fail closed."""
    return _native('get_blob', service, account, None, allow_ui=True)


def _native(action: str, service: str, account: str, secret: bytes | None, *, allow_ui=True):
    if sys.platform != "darwin":
        raise OSError("macOS Keychain is unavailable")
    import ctypes

    cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    security = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
    with _user_interaction(security, allow_ui):
        return _native_query(cf, security, action, service, account, secret, allow_ui)


def _native_query(cf, security, action, service, account, secret, allow_ui):
    import ctypes
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
    cf.CFDataCreate.restype = ctypes.c_void_p
    cf.CFDictionaryCreate.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
    ]
    cf.CFDictionaryCreate.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    cf.CFDataGetBytePtr.restype = ctypes.c_void_p
    security.SecItemDelete.argtypes = [ctypes.c_void_p]
    security.SecItemDelete.restype = ctypes.c_int32
    security.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.SecItemAdd.restype = ctypes.c_int32
    security.SecItemCopyMatching.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    security.SecItemCopyMatching.restype = ctypes.c_int32

    def const(library, name: str):
        value = ctypes.c_void_p.in_dll(library, name).value
        if not value:
            raise OSError("keychain_failed")
        return value

    def symbol(library, name: str):
        return ctypes.addressof(ctypes.c_char.in_dll(library, name))

    owned: list[int] = []
    keepalive: list[object] = []

    def cfstr(text: str):
        handle = cf.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100)
        if not handle:
            raise OSError("keychain_failed")
        owned.append(handle)
        return handle

    def cfdata(blob: bytes):
        buffer = ctypes.create_string_buffer(blob)
        keepalive.append(buffer)
        handle = cf.CFDataCreate(None, ctypes.cast(buffer, ctypes.c_void_p), len(blob))
        if not handle:
            raise OSError("keychain_failed")
        owned.append(handle)
        return handle

    def dictionary(pairs: list[tuple[int, int]]):
        count = len(pairs)
        keys = (ctypes.c_void_p * count)(*(key for key, _ in pairs))
        values = (ctypes.c_void_p * count)(*(value for _, value in pairs))
        keepalive.extend((keys, values))
        handle = cf.CFDictionaryCreate(
            None, keys, values, count, symbol(cf, "kCFTypeDictionaryKeyCallBacks"),
            symbol(cf, "kCFTypeDictionaryValueCallBacks"),
        )
        if not handle:
            raise OSError("keychain_failed")
        owned.append(handle)
        return handle

    try:
        base = [
            (const(security, "kSecClass"), const(security, "kSecClassGenericPassword")),
            (const(security, "kSecAttrService"), cfstr(service)),
            (const(security, "kSecAttrAccount"), cfstr(account)),
        ]
        if not allow_ui:
            base.append((const(security, 'kSecUseAuthenticationUI'), const(security, 'kSecUseAuthenticationUIFail')))
        if action == 'put_blob':
            security.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            security.SecItemUpdate.restype = ctypes.c_int32
            attributes = [(const(security, "kSecValueData"), cfdata(secret))]
            rc = security.SecItemUpdate(dictionary(base), dictionary(attributes))
            if rc == -25300:  # errSecItemNotFound
                rc = security.SecItemAdd(dictionary(base + attributes), None)
            if rc != 0:
                raise OSError('keychain_failed')
            return None
        if action == "add":
            assert secret is not None
            security.SecItemDelete(dictionary(base))
            attributes = base + [
                (const(security, "kSecValueData"), cfdata(secret)),
                (const(security, "kSecAttrAccessible"), const(security, "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly")),
            ]
            if security.SecItemAdd(dictionary(attributes), None) != 0:
                raise OSError("keychain_failed")
            return None
        query = base + [
            (const(security, "kSecReturnData"), const(cf, "kCFBooleanTrue")),
            (const(security, "kSecMatchLimit"), const(security, "kSecMatchLimitOne")),
        ]
        result = ctypes.c_void_p()
        rc = security.SecItemCopyMatching(dictionary(query), ctypes.byref(result))
        if rc == -25300 and action == "get_blob":
            return None
        if rc != 0 or not result.value:
            raise OSError("keychain_failed")
        owned.append(result.value)
        length = cf.CFDataGetLength(result.value)
        if (action != "get_blob" and length != 32) or length < 0 or length > 16 * 1024 * 1024:
            raise OSError("keychain_failed")
        return ctypes.string_at(cf.CFDataGetBytePtr(result.value), length)
    finally:
        for handle in reversed(owned):
            cf.CFRelease(handle)


def cli_runner():
    return subprocess.run

"""Credential vault: secrets encrypted with Windows DPAPI, bound to this Windows user.

Nothing is stored in plain text and nothing needs a master password: DPAPI keys the data to the
signed-in Windows account, so the file is useless if copied to another machine or account.
Used for OAuth tokens (Google, and later WhatsApp and others), never for the project's config.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

ENTROPY = b"jarvis-vault-v1"       # extra salt so other DPAPI users on this account cannot trivially read it


class VaultError(RuntimeError):
    pass


def _blob(data: bytes):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf, DATA_BLOB


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi(data, protect=True)


def dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi(data, protect=False)


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise VaultError("the credential vault needs Windows (DPAPI)")
    import ctypes
    src, keep, DATA_BLOB = _blob(data)
    ent, keep2, _ = _blob(ENTROPY)
    out = DATA_BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out))
    if not ok:
        raise VaultError("Windows could not " + ("encrypt" if protect else "decrypt") + " the vault data")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


class Vault:
    """A small encrypted key/value store. Values are JSON-serialisable."""

    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            from jarvis.core.store import default_data_dir
            path = default_data_dir() / "vault.bin"
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(dpapi_unprotect(self.path.read_bytes()).decode("utf-8"))
        except VaultError:
            raise
        except Exception as e:
            raise VaultError(f"the vault file is unreadable ({type(e).__name__})") from None

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = dpapi_protect(json.dumps(data).encode("utf-8"))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.path)          # atomic: a crash never leaves a half-written vault

    def get(self, key: str, default=None):
        with self._lock:
            return self._read().get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            data = self._read()
            data[key] = value
            self._write(data)

    def delete(self, key: str) -> bool:
        with self._lock:
            data = self._read()
            if key not in data:
                return False
            del data[key]
            self._write(data)
            return True

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._read())

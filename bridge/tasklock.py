"""OS-level exclusive locks for bridge tasks, shared by the worker and admin.py (stdlib only).

A lock is an open file handle holding a byte-range lock (msvcrt on Windows, flock elsewhere). The operating
system releases it automatically when the holder exits or is killed, so a crashed worker can never leave a
stale lock behind. The file content ({pid, at, owner}) is for diagnostics only; it never decides anything.
"""
import json
import os
import sys
import time
from pathlib import Path

_OFFSET = 1 << 20  # lock one byte far past the diagnostics, so other processes can still read them
_FLAGS = os.O_RDWR | getattr(os, "O_BINARY", 0)

if sys.platform == "win32":
    import msvcrt

    def _lock(fd):
        os.lseek(fd, _OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd):
        os.lseek(fd, _OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


class Lock:
    def __init__(self, path: Path, fd: int):
        self.path, self.fd = path, fd

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            _unlock(self.fd)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None
        if sys.platform == "win32":  # safe on Windows: deletion fails while anyone else has the file open
            try:
                self.path.unlink()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def acquire(path, owner: str = "worker") -> Lock | None:
    """Take the lock without waiting. Returns None if someone else holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, _FLAGS | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        _lock(fd)
    except OSError:
        os.close(fd)
        return None
    info = json.dumps({"pid": os.getpid(), "at": time.time(), "owner": owner}).encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, info.ljust(256))
    return Lock(path, fd)


def is_held(path) -> bool:
    try:
        fd = os.open(Path(path), _FLAGS)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # cannot even open it: treat as held (conservative)
    try:
        _lock(fd)
    except OSError:
        return True
    else:
        _unlock(fd)
        return False
    finally:
        os.close(fd)


def info(path) -> dict:
    try:
        return json.loads(Path(path).read_bytes().decode("utf-8", "replace").strip() or "{}")
    except (OSError, ValueError):
        return {}

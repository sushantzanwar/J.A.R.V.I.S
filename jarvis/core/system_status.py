"""Read-only CPU / memory / disk readings using only the standard library (ctypes on Windows)."""
import ctypes
import shutil
import sys
from ctypes import wintypes
from pathlib import Path

GB = 1024**3


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _ticks(ft: wintypes.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


class SystemMonitor:
    """Call snapshot() periodically; CPU load is the delta since the previous call."""

    def __init__(self, disk_path: str | None = None):
        self._disk_path = disk_path or Path.home().anchor or "C:\\"
        self._prev: tuple[int, int] | None = None

    def _cpu(self) -> float:
        if sys.platform != "win32":
            return 0.0
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return 0.0
        idle_t, total = _ticks(idle), _ticks(kernel) + _ticks(user)  # kernel time includes idle
        prev, self._prev = self._prev, (idle_t, total)
        if prev is None or total <= prev[1]:
            return 0.0
        return max(0.0, min(1.0, 1.0 - (idle_t - prev[0]) / (total - prev[1])))

    def _ram(self) -> tuple[float, float]:
        if sys.platform != "win32":
            return 0.0, 0.0
        ms = _MemoryStatus()
        ms.dwLength = ctypes.sizeof(ms)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return 0.0, 0.0
        return ms.dwMemoryLoad / 100.0, ms.ullTotalPhys / GB

    def snapshot(self) -> dict:
        ram, ram_total = self._ram()
        du = shutil.disk_usage(self._disk_path)
        return {
            "cpu": self._cpu(),
            "ram": ram,
            "ram_used_gb": ram * ram_total,
            "ram_total_gb": ram_total,
            "disk": du.used / du.total if du.total else 0.0,
            "disk_used_gb": du.used / GB,
            "disk_total_gb": du.total / GB,
        }

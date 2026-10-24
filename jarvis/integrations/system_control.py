"""Narrowly scoped Windows computer-control tools.

Provides safe, allowlist-only actuation:
- Media & volume control (Windows virtual key events via ctypes)
- Allowlisted application launcher (os.startfile, no arbitrary commands)
- System telemetry queries (battery status, uptime, active window)
- Screen locking and explicit refusal of destructive actions (shutdown/restart)

Zero external dependencies; standard library ctypes only.
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Any

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    VK_VOLUME_MUTE = 0xAD
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_UP = 0xAF
    VK_MEDIA_NEXT_TRACK = 0xB0
    VK_MEDIA_PREV_TRACK = 0xB1
    VK_MEDIA_STOP = 0xB2
    VK_MEDIA_PLAY_PAUSE = 0xB3

    class _SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_byte),
            ("BatteryFlag", ctypes.c_byte),
            ("BatteryLifePercent", ctypes.c_byte),
            ("SystemStatusFlag", ctypes.c_byte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]
else:
    _u32 = None  # type: ignore[assignment]
    _k32 = None  # type: ignore[assignment]
    VK_VOLUME_MUTE = 0xAD
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_UP = 0xAF
    VK_MEDIA_NEXT_TRACK = 0xB0
    VK_MEDIA_PREV_TRACK = 0xB1
    VK_MEDIA_STOP = 0xB2
    VK_MEDIA_PLAY_PAUSE = 0xB3


# Allowlisted desktop application targets.
# Maps normalized names and aliases to verified Windows shell targets.
ALLOWED_APPS: dict[str, str] = {
    "notepad": "notepad.exe",
    "calc": "calc.exe",
    "calculator": "calc.exe",
    "explorer": "explorer.exe",
    "files": "explorer.exe",
    "terminal": "wt.exe",
    "cmd": "cmd.exe",
    "settings": "ms-settings:",
    "taskmgr": "taskmgr.exe",
    "task manager": "taskmgr.exe",
}


def _send_vk(vk_code: int) -> bool:
    """Simulate a single virtual key press and release via Win32 keybd_event."""
    if _u32 is None:
        return False
    try:
        _u32.keybd_event(vk_code, 0, 0, 0)
        _u32.keybd_event(vk_code, 0, 2, 0)  # KEYEVENTF_KEYUP = 2
        return True
    except Exception:
        return False


class SystemControl:
    """Safe, scoped computer control actions."""

    def __init__(self, allowed_apps: dict[str, str] | None = None) -> None:
        self.allowed_apps = dict(allowed_apps if allowed_apps is not None else ALLOWED_APPS)

    # ---- Volume & Media Control ----

    def volume_mute(self) -> str:
        if _send_vk(VK_VOLUME_MUTE):
            return "Volume mute toggled."
        return "Mute action simulated (non-Windows platform)."

    def volume_up(self, steps: int = 2) -> str:
        count = max(1, min(10, steps))
        for _ in range(count):
            _send_vk(VK_VOLUME_UP)
            time.sleep(0.02)
        return f"Volume turned up (+{count})."

    def volume_down(self, steps: int = 2) -> str:
        count = max(1, min(10, steps))
        for _ in range(count):
            _send_vk(VK_VOLUME_DOWN)
            time.sleep(0.02)
        return f"Volume turned down (-{count})."

    def media_play_pause(self) -> str:
        if _send_vk(VK_MEDIA_PLAY_PAUSE):
            return "Media play/pause toggled."
        return "Media play/pause simulated (non-Windows platform)."

    def media_next(self) -> str:
        if _send_vk(VK_MEDIA_NEXT_TRACK):
            return "Skipped to next media track."
        return "Next track simulated (non-Windows platform)."

    def media_prev(self) -> str:
        if _send_vk(VK_MEDIA_PREV_TRACK):
            return "Returned to previous media track."
        return "Previous track simulated (non-Windows platform)."

    def media_stop(self) -> str:
        if _send_vk(VK_MEDIA_STOP):
            return "Media playback stopped."
        return "Media stop simulated (non-Windows platform)."

    # ---- Application Launcher ----

    def open_app(self, name: str) -> str:
        clean = name.strip().lower()
        target = self.allowed_apps.get(clean)
        if not target:
            allowed_list = ", ".join(sorted(set(self.allowed_apps.keys())))
            return (
                f"Application '{clean}' is not in the allowed list.\n"
                f"Allowed applications: {allowed_list}."
            )

        if hasattr(os, "startfile"):
            try:
                os.startfile(target)  # type: ignore[attr-defined]
                return f"Opened {clean} ({target})."
            except Exception as e:
                return f"Could not open {clean}: {e}"
        return f"Simulated opening {clean} ({target}) on non-Windows platform."

    # ---- System Information Queries ----

    def get_battery(self) -> str:
        if _k32 is None:
            return "Battery information unavailable on this platform."
        try:
            sps = _SYSTEM_POWER_STATUS()
            if not _k32.GetSystemPowerStatus(ctypes.byref(sps)):
                return "Could not retrieve power status from the system."
            percent = sps.BatteryLifePercent
            if percent == 255:
                return "No battery detected (desktop PC or power status unknown)."
            ac_status = sps.ACLineStatus
            charging_str = "plugged in" if ac_status == 1 else "on battery"
            return f"Battery at {percent}%, {charging_str}."
        except Exception as e:
            return f"Error querying battery: {e}"

    def get_uptime(self) -> str:
        if _k32 is None:
            return "System uptime unavailable on this platform."
        try:
            ms = _k32.GetTickCount64()
            seconds = int(ms / 1000)
            days, rem = divmod(seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, _ = divmod(rem, 60)
            parts = []
            if days > 0:
                parts.append(f"{days} day{'s' if days != 1 else ''}")
            if hours > 0:
                parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
            return f"System uptime: {', '.join(parts)}."
        except Exception as e:
            return f"Error querying uptime: {e}"

    def get_active_window(self) -> str:
        if _u32 is None:
            return "Active window information unavailable on this platform."
        try:
            hwnd = _u32.GetForegroundWindow()
            if not hwnd:
                return "No active foreground window found."
            buf = ctypes.create_unicode_buffer(512)
            _u32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value.strip()
            return f"Active window: '{title}'" if title else "Active window has no title."
        except Exception as e:
            return f"Error querying active window: {e}"

    # ---- Security & Protective Gating ----

    def lock_workstation(self) -> str:
        if _u32 is not None:
            try:
                if _u32.LockWorkStation():
                    return "Workstation locked."
            except Exception as e:
                return f"Could not lock workstation: {e}"
        return "Workstation lock simulated."

    def protected_action(self, action_name: str) -> str:
        return (
            f"Action '{action_name}' is protected and requires explicit human approval.\n"
            "Destructive actions (shutdown, restart, format) cannot be run automatically."
        )

    # ---- Command Router ----

    def execute_command(self, query: str) -> str | None:
        """Parse natural language command and execute the appropriate safe tool.
        Returns a response string if handled, or None if the query is not a system tool command.
        """
        q = query.strip().lower()

        # 1. Volume
        if q in ("mute", "unmute", "mute volume", "toggle mute", "silence audio"):
            return self.volume_mute()
        if any(q.startswith(w) or q == w for w in ("volume up", "turn up volume", "raise volume", "louder")):
            return self.volume_up()
        if any(q.startswith(w) or q == w for w in ("volume down", "turn down volume", "lower volume", "quieter")):
            return self.volume_down()

        # 2. Media
        if q in ("play", "pause", "play music", "pause music", "toggle music", "resume music"):
            return self.media_play_pause()
        if q in ("next track", "next song", "skip track", "skip song"):
            return self.media_next()
        if q in ("prev track", "previous track", "prev song", "previous song"):
            return self.media_prev()
        if q in ("stop music", "stop audio", "stop playback"):
            return self.media_stop()

        # 3. Application launcher: "open <app>" or "launch <app>"
        m = re.match(r"^(?:open|launch|start)\s+([a-zA-Z0-9_\- ]+)$", q)
        if m:
            target = m.group(1).strip()
            # If target looks like a question or command outside launcher
            if target not in ("status", "tasks", "queue", "time", "date", "help", "about"):
                return self.open_app(target)

        # 4. System telemetry queries
        if "battery" in q or "power status" in q:
            return self.get_battery()
        if "uptime" in q:
            return self.get_uptime()
        if "active window" in q or "foreground window" in q or "current window" in q:
            return self.get_active_window()

        # 5. Security & protected actions
        if q in ("lock pc", "lock computer", "lock screen", "lock workstation"):
            return self.lock_workstation()
        if any(w in q for w in ("shutdown", "shut down", "restart", "reboot")):
            return self.protected_action("system shutdown/restart")

        return None

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
import shutil
import sys
import time
from urllib.parse import quote_plus

from jarvis.integrations.process import run_quiet
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


# Device-query and protected-action phrasings, anchored on the whole utterance. Bare substring
# matching used to intercept any sentence that merely mentioned a keyword ("summarise the battery
# article", "restart the deployment script"), so each pattern must now match end to end.
_END = r"\s*\??$"
BATTERY_RE = re.compile(
    r"^(?:(?:what|what's|whats|how|how's|hows)\s+(?:is\s+|are\s+)?)?(?:the\s+|my\s+)?"
    r"(?:battery|power)(?:\s+(?:level|status|percent|percentage|charge|remaining|left))?" + _END)
UPTIME_RE = re.compile(
    r"^(?:(?:what|what's|whats)\s+(?:is\s+)?)?(?:the\s+|my\s+|system\s+)?uptime" + _END)
UPTIME_LONG_RE = re.compile(
    r"^how\s+long\s+(?:has|have)\s+(?:the\s+|my\s+)?(?:pc|computer|machine|system)\s+"
    r"been\s+(?:up|running)" + _END)
ACTIVE_WINDOW_RE = re.compile(
    r"^(?:(?:what|what's|whats|which)\s+(?:is\s+)?)?(?:the\s+|my\s+)?"
    r"(?:active|foreground|current|focused)\s+window" + _END)
SHUTDOWN_RE = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+)*"
    r"(?:shut\s*down|power\s*off|turn\s+off)"
    r"(?:\s+(?:the\s+|my\s+)?(?:pc|computer|machine|system|laptop|windows))?"
    r"(?:\s+(?:now|immediately|please))?" + _END)
RESTART_RE = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+)*"
    r"(?:restart|reboot)"
    r"(?:\s+(?:the\s+|my\s+)?(?:pc|computer|machine|system|laptop|windows))?"
    r"(?:\s+(?:now|immediately|please))?" + _END)
SLEEP_RE = re.compile(
    r"^(?:please\s+)?(?:(?:go|going)\s+to\s+sleep|sleep(?:\s+down)?|sleep\s+mode|hibernate)"
    r"(?:\s+(?:the\s+|my\s+|this\s+)?(?:pc|computer|laptop|machine|system))?\s*\??$"
    r"|^(?:please\s+)?(?:put|set|send|switch|turn)\s+(?:the\s+|my\s+|this\s+)?(?:pc|computer|laptop|machine|system)"
    r"\s+(?:to|in|into|on)\s+(?:sleep|hibernate)(?:\s+mode)?\s*\??$")
SIGNOUT_RE = re.compile(
    r"^(?:sign|log)\s*(?:me\s+)?(?:out|off)(?:\s+of\s+windows)?" + _END)
CANCEL_POWER_RE = re.compile(
    r"^(?:cancel|abort|stop)\s+(?:the\s+)?(?:shut\s*down|shutdown|restart|reboot)" + _END)
OPEN_RE = re.compile(
    r"^(?:open|launch|start|run|turn\s+on|go\s+to|play)\s+(?:the\s+|my\s+)?(.+?)"
    r"(?:\s+(?:app|application|website|site|please))?\s*$")


# ---- Full PC control -------------------------------------------------------
# The allowlist and the shutdown refusal were removed deliberately, at the owner's
# request, so Jarvis can act on any spoken instruction. Launching still never goes
# through a shell, and power actions use a fixed argv, so there is no command
# injection; what changed is WHAT is permitted, not HOW it is run.
#
# JARVIS_POWER_DELAY (seconds, default 10) is the grace period before a shutdown or
# restart actually happens. Say "cancel shutdown" inside that window to abort.
# Set it to 0 for immediate action.

WEBSITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
    "maps": "https://maps.google.com",
    "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com",
    "whatsapp": "https://web.whatsapp.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "amazon": "https://www.amazon.in",
    "wikipedia": "https://www.wikipedia.org",
    "drive": "https://drive.google.com",
    "linkedin": "https://www.linkedin.com",
    "instagram": "https://www.instagram.com",
}


# A target carrying switches, pipes, redirects or separators is a command line, not an
# application name. Those are never launched.
_LOOKS_LIKE_COMMAND = re.compile(r"[&|;<>^%]|\s[-/][A-Za-z]|\.\.[\/]")


def _power_delay() -> int:
    try:
        return max(0, int(os.environ.get("JARVIS_POWER_DELAY", "10")))
    except ValueError:
        return 10


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

    # ---- Launching: websites, installed apps, anything Windows can resolve ----

    def open_target(self, name: str) -> str:
        """Open a website, an installed application, or any path Windows can resolve."""
        clean = name.strip().strip('"').strip("'")
        low = clean.lower()
        if not low:
            return "Nothing to open."

        if low.startswith(("http://", "https://")) and " " not in clean:
            return self._launch(clean, f"Opened {clean}.")

        # Full access means "open anything you have", not "run an arbitrary command line".
        # ShellExecute will happily accept a string carrying switches, so a target that
        # looks like a command line is searched for instead of launched.
        if _LOOKS_LIKE_COMMAND.search(clean):
            return self._launch(f"https://www.google.com/search?q={quote_plus(clean)}",
                                f"That looked like a command line rather than an app, "
                                f"so I searched the web for '{low}' instead.")

        site = WEBSITES.get(low) or WEBSITES.get(low.replace(" ", ""))
        if site:
            return self._launch(site, f"Opened {low} in your browser.")

        if low.startswith(("http://", "https://")):
            return self._launch(clean, f"Opened {clean}.")
        if "." in low and " " not in low:
            return self._launch("https://" + clean, f"Opened https://{clean}.")

        target = self.allowed_apps.get(low)
        if target:
            return self._launch(target, f"Opened {low} ({target}).")

        exe = shutil.which(clean) or shutil.which(low)
        if exe:
            return self._launch(exe, f"Opened {low} ({exe}).")

        hit = self._launch(clean, f"Opened {low}.")
        if not hit.startswith("Could not"):
            return hit
        return self._launch(f"https://www.google.com/search?q={quote_plus(clean)}",
                            f"I could not find an app called '{low}', so I searched the web for it.")

    def open_app(self, name: str) -> str:
        """Backwards-compatible alias; open_target handles apps, sites and paths."""
        return self.open_target(name)

    @staticmethod
    def _launch(target: str, ok_message: str) -> str:
        if not hasattr(os, "startfile"):
            return f"Simulated opening {target} on non-Windows platform."
        try:
            os.startfile(target)   # type: ignore[attr-defined]
            return ok_message
        except Exception as e:
            return f"Could not open {target}: {e}"

    # ---- Power: shutdown, restart, sleep, sign out ----

    @staticmethod
    def _run(argv: list) -> tuple:
        """Fixed argv, never a shell."""
        try:
            p = run_quiet(argv, capture_output=True, text=True, timeout=15)
            return p.returncode == 0, (p.stderr or p.stdout or "").strip()
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def shutdown(self) -> str:
        delay = _power_delay()
        ok, detail = self._run(["shutdown", "/s", "/t", str(delay)])
        if not ok:
            return f"Could not shut down: {detail[:120]}"
        if delay:
            return f"Shutting down in {delay} seconds. Say 'cancel shutdown' to stop it."
        return "Shutting down now."

    def restart(self) -> str:
        delay = _power_delay()
        ok, detail = self._run(["shutdown", "/r", "/t", str(delay)])
        if not ok:
            return f"Could not restart: {detail[:120]}"
        if delay:
            return f"Restarting in {delay} seconds. Say 'cancel shutdown' to stop it."
        return "Restarting now."

    def cancel_power_action(self) -> str:
        ok, detail = self._run(["shutdown", "/a"])
        if ok:
            return "Cancelled the pending shutdown."
        return "There was no pending shutdown to cancel."

    def sign_out(self) -> str:
        ok, detail = self._run(["shutdown", "/l"])
        return "Signing out." if ok else f"Could not sign out: {detail[:120]}"

    def sleep_pc(self) -> str:
        ok, detail = self._run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        return "Going to sleep." if ok else f"Could not sleep: {detail[:120]}"

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
        if _u32 is None:
            return "Could not lock workstation: the Windows user32 API is unavailable here."
        try:
            if _u32.LockWorkStation():
                return "Workstation locked."
        except Exception as e:
            return f"Could not lock workstation: {e}"
        return "Could not lock workstation: the system refused the request."

    def protected_action(self, action_name: str) -> str:
        return (
            f"Action '{action_name}' is protected and requires explicit human approval.\n"
            "Destructive actions (shutdown, restart, format) cannot be run automatically."
        )

    # ---- Command Router ----

    def resolve(self, query: str) -> tuple[str, dict] | None:
        """Map an utterance to (method name, kwargs) WITHOUT executing anything.

        The single grammar for device commands: execute_command runs what this returns,
        and the agent planner uses it to turn "open youtube and then mute" into steps.
        """
        # Speech recognisers end transcripts with ". ", "?" or "!", which no pattern expects.
        q = query.strip().lower().rstrip(" .!?,;")

        # 1. Volume
        if q in ("mute", "unmute", "mute volume", "toggle mute", "silence audio"):
            return "volume_mute", {}
        if any(q.startswith(w) or q == w for w in ("volume up", "turn up volume", "raise volume", "louder")):
            return "volume_up", {}
        if any(q.startswith(w) or q == w for w in ("volume down", "turn down volume", "lower volume", "quieter")):
            return "volume_down", {}

        # 2. Media
        if q in ("play", "pause", "play music", "pause music", "toggle music", "resume music"):
            return "media_play_pause", {}
        if q in ("next track", "next song", "skip track", "skip song"):
            return "media_next", {}
        if q in ("prev track", "previous track", "prev song", "previous song"):
            return "media_prev", {}
        if q in ("stop music", "stop audio", "stop playback"):
            return "media_stop", {}

        # 3. Power and session control (full access, by owner's request). Checked before
        #    the launcher so "go to sleep" is never read as "open an app called sleep".
        if CANCEL_POWER_RE.search(q):
            return "cancel_power_action", {}
        if q in ("lock pc", "lock computer", "lock screen", "lock workstation", "lock the workstation", "lock"):
            return "lock_workstation", {}
        if SLEEP_RE.search(q):
            return "sleep_pc", {}
        if SIGNOUT_RE.search(q):
            return "sign_out", {}
        if RESTART_RE.search(q):
            return "restart", {}
        if SHUTDOWN_RE.search(q):
            return "shutdown", {}

        # 4. Launcher: apps, websites, anything Windows can resolve
        m = OPEN_RE.match(q)
        if m:
            target = m.group(1).strip()
            if target and target not in ("status", "tasks", "queue", "time", "date", "help", "about"):
                return "open_target", {"name": target}

        # 5. System telemetry queries
        if BATTERY_RE.match(q):
            return "get_battery", {}
        if UPTIME_RE.match(q) or UPTIME_LONG_RE.match(q):
            return "get_uptime", {}
        if ACTIVE_WINDOW_RE.match(q):
            return "get_active_window", {}

        return None

    def execute_command(self, query: str) -> str | None:
        """Run the command an utterance names, or return None if it is not a device command."""
        call = self.resolve(query)
        if call is None:
            return None
        name, kwargs = call
        return getattr(self, name)(**kwargs)

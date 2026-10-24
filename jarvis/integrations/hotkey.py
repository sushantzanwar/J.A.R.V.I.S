"""Global (system-wide) push-to-talk hotkey.

Hold-to-talk needs both key-down and key-up. RegisterHotKey reports only the press, so
the chord's state is polled with GetAsyncKeyState instead. Polling deliberately avoids
installing a low-level keyboard hook: no keystroke is observed, intercepted or recorded,
and only whether the configured chord is currently held is ever read.

The chord is read but never swallowed, so the keys keep working in other applications.

Zero external dependencies; standard library ctypes only.
"""
from __future__ import annotations

import sys
import threading
from typing import Callable

if sys.platform == "win32":
    import ctypes

    _u32 = ctypes.WinDLL("user32", use_last_error=True)
else:
    _u32 = None  # type: ignore[assignment]

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12      # Alt
VK_SPACE = 0x20

# Ctrl+Alt+Space: deliberately not the in-app Ctrl+Space, which many IMEs and editors use.
DEFAULT_CHORD: tuple[int, ...] = (VK_CONTROL, VK_MENU, VK_SPACE)
POLL_SECONDS = 0.05
_DOWN = 0x8000


class GlobalHotkey:
    """Watch a key chord and report hold and release, regardless of which window has focus.

    ``key_state`` is injectable so tests can drive the chord without real key presses.
    """

    def __init__(self, on_press: Callable[[], None], on_release: Callable[[], None],
                 chord: tuple[int, ...] = DEFAULT_CHORD, interval: float = POLL_SECONDS,
                 key_state: Callable[[int], int] | None = None):
        self._on_press = on_press
        self._on_release = on_release
        self._chord = tuple(chord)
        self._interval = interval
        self._key_state = key_state or self._async_key_state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._held = False

    @staticmethod
    def _async_key_state(vk: int) -> int:
        if _u32 is None:
            return 0
        return _u32.GetAsyncKeyState(vk)

    @staticmethod
    def is_available() -> bool:
        return _u32 is not None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def held(self) -> bool:
        return self._held

    def chord_held(self) -> bool:
        """True only while every key of the chord is down."""
        try:
            return all(self._key_state(vk) & _DOWN for vk in self._chord)
        except Exception:
            return False

    def start(self) -> bool:
        if not self.is_available() or self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-hotkey", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._held = False

    def poll_once(self) -> None:
        """One edge-detection step. Separated from the loop so it can be tested directly."""
        held = self.chord_held()
        if held == self._held:
            return
        self._held = held
        callback = self._on_press if held else self._on_release
        try:
            callback()
        except Exception:
            # A failing callback must never kill the watcher thread; the next edge still fires.
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._interval)

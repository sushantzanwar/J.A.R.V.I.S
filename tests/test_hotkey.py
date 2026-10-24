"""Global push-to-talk hotkey: edge detection, lifecycle and Backend wiring.

The chord is driven through an injected key-state function, so no real key is pressed and
no test depends on which window has focus.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.integrations.hotkey import (DEFAULT_CHORD, VK_CONTROL, VK_MENU, VK_SPACE,
                                        GlobalHotkey)
from jarvis.ui.backend import Backend
from tests.qt_helpers import gui_app, pump

DOWN = 0x8000


class FakeKeyboard:
    """Reports the chord as held only while the test says so."""

    def __init__(self):
        self.down: set[int] = set()

    def state(self, vk: int) -> int:
        return DOWN if vk in self.down else 0

    def press(self, *vks: int) -> None:
        self.down.update(vks)

    def release(self, *vks: int) -> None:
        self.down.difference_update(vks)


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        self.kb = FakeKeyboard()
        self.events: list[str] = []
        self.hk = GlobalHotkey(lambda: self.events.append("press"),
                               lambda: self.events.append("release"),
                               key_state=self.kb.state)

    def test_default_chord_is_ctrl_alt_space(self):
        self.assertEqual(DEFAULT_CHORD, (VK_CONTROL, VK_MENU, VK_SPACE))

    def test_partial_chord_does_not_fire(self):
        self.kb.press(VK_CONTROL, VK_SPACE)      # Alt missing: not the chord
        self.hk.poll_once()
        self.assertEqual(self.events, [])
        self.assertFalse(self.hk.held)

    def test_press_and_release_fire_once_each(self):
        self.kb.press(*DEFAULT_CHORD)
        self.hk.poll_once()
        self.hk.poll_once()                      # still held: no repeat
        self.assertEqual(self.events, ["press"])
        self.assertTrue(self.hk.held)

        self.kb.release(VK_SPACE)
        self.hk.poll_once()
        self.hk.poll_once()
        self.assertEqual(self.events, ["press", "release"])
        self.assertFalse(self.hk.held)

    def test_repeated_hold_cycles(self):
        for _ in range(3):
            self.kb.press(*DEFAULT_CHORD)
            self.hk.poll_once()
            self.kb.release(*DEFAULT_CHORD)
            self.hk.poll_once()
        self.assertEqual(self.events, ["press", "release"] * 3)

    def test_failing_callback_does_not_break_the_watcher(self):
        hk = GlobalHotkey(mock.Mock(side_effect=RuntimeError("boom")),
                          lambda: self.events.append("release"), key_state=self.kb.state)
        self.kb.press(*DEFAULT_CHORD)
        hk.poll_once()                           # must not raise
        self.kb.release(*DEFAULT_CHORD)
        hk.poll_once()
        self.assertEqual(self.events, ["release"])

    def test_unreadable_key_state_is_not_held(self):
        hk = GlobalHotkey(lambda: None, lambda: None,
                          key_state=mock.Mock(side_effect=OSError("no user32")))
        self.assertFalse(hk.chord_held())

    def test_start_and_stop_lifecycle(self):
        self.assertFalse(self.hk.running)
        self.assertTrue(self.hk.start())
        self.assertTrue(self.hk.running)
        self.assertFalse(self.hk.start())        # already running
        self.hk.stop()
        self.assertFalse(self.hk.running)

    def test_start_refused_when_unavailable(self):
        with mock.patch("jarvis.integrations.hotkey._u32", None):
            self.assertFalse(GlobalHotkey.is_available())
            self.assertFalse(GlobalHotkey(lambda: None, lambda: None).start())


class HotkeyBackendWiringTests(unittest.TestCase):
    """The injected factory connects the chord to recording; a bare Backend starts no thread."""

    @classmethod
    def setUpClass(cls):
        cls.app = gui_app()

    def _backend(self, tasks_dir, **kw):
        recorder = mock.MagicMock()
        recorder.start.return_value = True
        recorder.stop.return_value = bytes(6400)   # 0.2s of silence
        recorder.current_level = 0.4
        recognizer = mock.MagicMock()
        recognizer.transcribe_wav.return_value = ("status", 0.95)
        return Backend(tasks_dir=tasks_dir, audio_recorder=recorder,
                       speech_recognizer=recognizer, **kw)

    def test_chord_callbacks_drive_recording(self):
        captured = {}

        def factory(on_press, on_release):
            captured.update(on_press=on_press, on_release=on_release, hk=mock.Mock())
            return captured["hk"]

        with tempfile.TemporaryDirectory() as td:
            tasks_dir = Path(td) / "tasks"
            tasks_dir.mkdir()
            b = self._backend(tasks_dir, hotkey_factory=factory)
            try:
                captured["hk"].start.assert_called_once_with()

                # Emitted from the watcher thread in production; queued onto the UI thread.
                captured["on_press"]()
                pump(lambda: b.voiceRecording, timeout=2.0)
                self.assertTrue(b.voiceRecording)

                captured["on_release"]()
                pump(lambda: not b.voiceRecording, timeout=2.0)
                self.assertFalse(b.voiceRecording)
            finally:
                b.shutdown()
            captured["hk"].stop.assert_called_once_with()

    def test_no_hotkey_without_a_factory(self):
        with tempfile.TemporaryDirectory() as td:
            tasks_dir = Path(td) / "tasks"
            tasks_dir.mkdir()
            b = self._backend(tasks_dir)
            try:
                self.assertIsNone(b._hotkey)
            finally:
                b.shutdown()


if __name__ == "__main__":
    unittest.main()

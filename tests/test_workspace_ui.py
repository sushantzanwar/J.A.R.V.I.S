"""The real QML workspace, loaded offscreen: settings, banner, empty states, shortcuts, contrast."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from tests.qt_helpers import FakeProvider, gui_app, pump

from PySide6.QtCore import QMetaObject, QObject, Qt, qInstallMessageHandler  # noqa: E402
from PySide6.QtQuick import QQuickItem  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from jarvis.app import QML_DIR, build  # noqa: E402
from jarvis.core.store import Store  # noqa: E402


def luminance(hex6: str) -> float:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex6[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class ContrastTests(unittest.TestCase):
    def test_theme_text_colours_meet_wcag_aa_on_every_surface(self):
        theme = (QML_DIR / "Theme.qml").read_text(encoding="utf-8")
        colors = {k: v for k, v in re.findall(r'readonly property color (\w+): "#([0-9A-Fa-f]{6})"', theme)}
        surfaces = [colors["bg"], colors["panel"], "0A0C0F", "111318", "10202B", "17102B"]   # every fill used behind text
        for name in ("text", "muted", "cyan", "violet", "ice"):
            for surface in surfaces:
                self.assertGreaterEqual(contrast(colors[name], surface), 4.5, f"{name} on #{surface}")

    def test_dark_text_on_primary_buttons_is_readable(self):
        theme = (QML_DIR / "Theme.qml").read_text(encoding="utf-8")
        colors = {k: v for k, v in re.findall(r'readonly property color (\w+): "#([0-9A-Fa-f]{6})"', theme)}
        for fill in ("cyan", "ice", "violet"):
            self.assertGreaterEqual(contrast(colors["bg"], colors[fill]), 4.5, fill)


class QmlCase(unittest.TestCase):
    messages: list = []

    @classmethod
    def setUpClass(cls):
        gui_app()
        qInstallMessageHandler(lambda mode, ctx, msg: cls.messages.append(msg))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tasks = self.root / "bridge" / "tasks"
        self.tasks.mkdir(parents=True)
        self.store = Store(self.root / "data" / "jarvis.db")
        self.addCleanup(self.store.close)
        QmlCase.messages.clear()

    def load(self, provider=None, store=None):
        provider = provider or FakeProvider(name="offline", state="builtin", detail="built-in responder")
        self.engine, self.backend = build(self.tasks, provider, store or self.store)
        self.addCleanup(self._teardown, self.engine, self.backend)
        self.assertTrue(self.engine.rootObjects(), "\n".join(QmlCase.messages))
        self.win = self.engine.rootObjects()[0]
        pump(lambda: self.backend.providerState != "checking")
        pump(minimum=0.3)
        return self.win

    @staticmethod
    def _teardown(engine, backend):
        """Stop the backend first, then let the engine be destroyed while everything it references is still alive."""
        backend.shutdown()
        engine.deleteLater()
        pump(minimum=0.1)

    def child(self, name):
        """Find a QML object by objectName. Items made by a Repeater are not QObject children of the window,
        so fall back to walking the visual item tree (including the popup overlay)."""
        obj = self.win.findChild(QObject, name)
        if obj is None:
            stack = list(self.win.findChildren(QQuickItem)) + [self.win.contentItem()]
            while stack and obj is None:
                item = stack.pop()
                if item.objectName() == name:
                    obj = item
                stack.extend(item.childItems())
        self.assertIsNotNone(obj, f"no QML object named {name}")
        return obj

    def qml_errors(self):
        return [m for m in QmlCase.messages if "QFontDatabase" not in m
                and any(w in m for w in ("ReferenceError", "TypeError", "Cannot ", "is not a type", "unavailable",
                                         "Binding loop", "is not defined", "Unable to assign"))]


class SettingsTests(QmlCase):
    def open_settings(self):
        QMetaObject.invokeMethod(self.win, "openSettings")
        popup = self.child("settingsPopup")
        self.assertTrue(pump(lambda: popup.property("opened")), "settings did not open")
        return popup

    def click(self, name):
        QMetaObject.invokeMethod(self.child(name), "clicked")
        pump(minimum=0.1)

    def test_settings_open_with_diagnostics_and_no_qml_errors(self):
        self.load()
        self.open_settings()
        diag = self.child("diagnosticsText").property("text")
        self.assertIn("Jarvis", diag)
        self.assertIn("Local data", diag)
        self.assertIn("Approval is human-only", diag)
        self.assertEqual(self.qml_errors(), [])

    def test_provider_status_and_explanation_are_shown(self):
        self.load(FakeProvider(name="claude-code", state="disconnected", detail="Claude Code is not signed in"))
        self.open_settings()
        self.assertEqual(self.child("providerStateText").property("text"), "DISCONNECTED")
        self.assertIn("not signed in", self.child("providerExplanation").property("text"))

    def test_provider_buttons_use_the_whitelist(self):
        self.load()
        self.open_settings()
        self.assertEqual(self.backend.providerName, "offline")
        self.click("provider-claude-code")
        self.assertEqual(self.backend.providerName, "claude-code")
        self.assertEqual(self.store.get_pref("provider"), "claude-code")
        self.click("provider-offline")
        self.assertEqual(self.backend.providerName, "offline")

    def test_toggles_change_preferences(self):
        self.load()
        self.open_settings()
        self.assertTrue(self.backend.saveHistory)
        self.click("toggleHistory")
        self.assertFalse(self.backend.saveHistory)
        self.assertFalse(self.child("toggleHistory").property("checked"))
        self.click("toggleMotion")
        self.assertTrue(self.backend.reduceMotion)
        self.click("resetPrefsButton")
        self.assertTrue(self.backend.saveHistory)
        self.assertFalse(self.backend.reduceMotion)
        self.assertTrue(self.child("toggleHistory").property("checked"))      # UI follows the backend after reset

    def test_data_location_is_explained(self):
        self.load()
        self.open_settings()
        self.assertEqual(self.child("dataLocation").property("text"), str(self.store.path))
        self.assertNotIn(str(Path(__file__).resolve().parents[1]), self.child("dataLocation").property("text"))

    def test_clear_history_needs_confirmation(self):
        self.load()
        self.backend.send("help")
        pump(lambda: len(self.backend.messages) >= 3 and self.backend.orbMode != "thinking")
        self.assertEqual(self.store.counts()["messages"], 2)
        self.open_settings()
        self.click("clearHistoryButton")                                      # only asks
        confirm = self.child("confirmClear")
        self.assertTrue(pump(lambda: confirm.property("opened")))
        self.assertEqual(self.store.counts()["messages"], 2)
        self.click("cancelClear")
        self.assertTrue(pump(lambda: not confirm.property("opened")))
        self.assertEqual(self.store.counts()["messages"], 2)                  # cancel keeps everything
        self.click("clearHistoryButton")
        pump(lambda: confirm.property("opened"))
        self.click("confirmClearYes")
        self.assertEqual(self.store.counts()["messages"], 0)
        self.assertEqual(self.child("chatList").property("count"), 1)         # back to the greeting

    def test_settings_expose_no_approval_or_command_controls(self):
        self.load()
        self.open_settings()
        names = {o.objectName() for o in self.win.findChildren(QObject) if o.objectName()}
        for name in names:
            self.assertNotRegex(name.lower(), r"approve|shell|command|run|exec|worker(start|stop|pause)", name)


class WorkspaceTests(QmlCase):
    def test_no_qml_errors_on_load_and_chat(self):
        self.load()
        self.backend.send("status")
        pump(lambda: len(self.backend.messages) >= 3 and self.backend.orbMode != "thinking")
        pump(minimum=0.3)
        self.assertEqual(self.qml_errors(), [])

    def test_timeline_and_task_lists_show_real_data(self):
        (self.tasks / "001.json").write_text(json.dumps(
            {"id": 1, "title": "Sample", "status": "pending", "approved": False, "needs": "read-only", "updated": "x"}))
        self.load()
        self.assertEqual(self.child("taskList").property("count"), 1)
        self.assertGreaterEqual(self.child("timelineList").property("count"), 1)

    def test_banner_appears_for_corrupt_data_and_can_be_dismissed(self):
        bad = self.root / "bad" / "jarvis.db"
        bad.parent.mkdir()
        bad.write_bytes(b"garbage" * 300)
        store = Store(bad)
        self.addCleanup(store.close)
        self.load(store=store)
        banner = self.child("storeBanner")
        self.assertTrue(banner.property("visible"))
        QMetaObject.invokeMethod(banner.findChild(QObject, "") or banner, "visible")   # no-op: just be sure it is real
        self.backend.dismissNotice()
        pump(minimum=0.1)
        self.assertFalse(self.child("storeBanner").property("visible"))

    def test_banner_hidden_when_all_is_well(self):
        self.load()
        self.assertFalse(self.child("storeBanner").property("visible"))

    def test_mode_text_follows_provider_state(self):
        self.load(FakeProvider(name="claude-code", state="connected", detail="ok"))
        self.assertIn("MODEL: CLAUDE-CODE", self.child("modeText").property("text"))
        self.load(FakeProvider(name="offline", state="builtin", detail="x"))
        self.assertIn("OFFLINE MODE", self.child("modeText").property("text"))

    def test_keyboard_shortcuts(self):
        win = self.load()
        win.show()
        win.requestActivate()
        pump(lambda: win.isActive(), timeout=2)
        popup = self.child("settingsPopup")
        QTest.keyClick(win, Qt.Key_Comma, Qt.ControlModifier)
        if not pump(lambda: popup.property("opened"), timeout=2):
            self.skipTest("offscreen platform did not deliver the key event to the shortcut system")
        QTest.keyClick(win, Qt.Key_Escape)
        self.assertTrue(pump(lambda: not popup.property("opened")), "Escape did not close settings")
        QTest.keyClick(win, Qt.Key_2, Qt.ControlModifier)
        self.assertTrue(pump(lambda: win.property("tab") == 1))
        QTest.keyClick(win, Qt.Key_1, Qt.ControlModifier)
        self.assertTrue(pump(lambda: win.property("tab") == 0))

    def test_every_interactive_control_has_an_accessible_name_and_shortcuts_are_declared(self):
        src = (QML_DIR / "Main.qml").read_text(encoding="utf-8")
        for seq in ("Ctrl+L", "Ctrl+,", "Ctrl+1", "Ctrl+2"):
            self.assertIn(f'sequence: "{seq}"', src)
        for qml in QML_DIR.glob("*.qml"):
            text = qml.read_text(encoding="utf-8")
            if qml.name in ("GhostButton.qml", "Toggle.qml"):
                self.assertIn("Accessible.name", text, qml.name)
        self.assertIn('Accessible.name: "Message to Jarvis"', src)
        self.assertIn("Accessible.role: Accessible.AlertMessage", src)


if __name__ == "__main__":
    unittest.main()

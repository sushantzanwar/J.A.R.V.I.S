"""Loads the real QML interface offscreen, drives the chat, and renders a screenshot."""
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, qInstallMessageHandler  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from jarvis.app import build  # noqa: E402

SHOT = os.environ.get("JARVIS_SHOT")  # optional path to save a screenshot


def pump(ms: int):
    import time
    end = time.time() + ms / 1000
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = []
        qInstallMessageHandler(lambda mode, ctx, msg: cls.messages.append(msg))
        cls.app = QGuiApplication.instance() or QGuiApplication([])
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        for i, (st, ap) in enumerate([("done", True), ("pending", False)], 1):
            (d / f"{i:03d}.json").write_text(json.dumps(
                {"id": i, "title": f"Sample task {i}", "status": st, "approved": ap,
                 "needs": "read-only", "updated": "now"}), encoding="utf-8")
        cls.engine, cls.backend = build(d)

    @classmethod
    def tearDownClass(cls):
        cls.backend.shutdown()
        cls.engine.deleteLater()
        cls.tmp.cleanup()

    def test_01_loads_without_qml_errors(self):
        self.assertTrue(self.engine.rootObjects(), "\n".join(self.messages))
        pump(500)
        # the offscreen platform plugin warns about its (absent) font directory; not a QML problem
        bad = [m for m in self.messages if "QFontDatabase" not in m
               and any(w in m for w in ("Error", "error", "ReferenceError", "TypeError", "unavailable", "Cannot"))]
        self.assertEqual(bad, [])

    def test_02_task_list_is_read_only_view(self):
        win = self.engine.rootObjects()[0]
        lst = win.findChild(QObject, "taskList")
        self.assertEqual(lst.property("count"), 2)
        self.assertEqual(self.backend.awaitingCount, 1)

    def test_03_chat_roundtrip_and_orb_modes(self):
        win = self.engine.rootObjects()[0]
        chat = win.findChild(QObject, "chatList")
        self.assertEqual(chat.property("count"), 1)
        self.backend.send("status")
        self.assertEqual(self.backend.orbMode, "thinking")
        pump(900)
        self.assertEqual(chat.property("count"), 3)
        self.assertEqual(self.backend.orbMode, "speaking")
        self.assertIn("CPU", self.backend.messages[-1]["text"])
        pump(1900)
        self.assertEqual(self.backend.orbMode, "idle")

    def test_04_render_screenshot(self):
        win = self.engine.rootObjects()[0]
        pump(600)
        img = win.grabWindow()
        self.assertGreater(img.width(), 900)
        colors = {img.pixel(x, y) for x in range(0, img.width(), 37) for y in range(0, img.height(), 37)}
        self.assertGreater(len(colors), 8, "window rendered blank")
        if SHOT:
            img.save(SHOT)
        bad = [m for m in self.messages if "ReferenceError" in m or "TypeError" in m]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()

"""Backend chat routing with the provider: offline, disconnected, failing and healthy models."""
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from jarvis.core.providers import CONNECTED, DISCONNECTED, ERROR, ModelProvider, ProviderError, ProviderStatus  # noqa: E402
from jarvis.ui.backend import Backend  # noqa: E402


class Fake(ModelProvider):
    name = "fake"

    def __init__(self, state, answer="model says hi", exc=None):
        self.state, self.answer, self.exc, self.prompts = state, answer, exc, []

    def status(self):
        return ProviderStatus(self.state, "fake detail")

    def generate(self, prompt):
        self.prompts.append(prompt)
        if self.exc:
            raise self.exc
        return self.answer


def pump(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


class BackendProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # must be a QGuiApplication: test_ui.py shares this process-wide instance
        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def make(self, provider):
        d = tempfile.mkdtemp()
        b = Backend(Path(d), provider)
        self.assertTrue(pump(lambda: b.providerState != "checking"))
        return b

    def ask(self, b, text):
        n = len(b.messages)
        b.send(text)
        self.assertTrue(pump(lambda: len(b.messages) >= n + 2 and b.orbMode != "thinking"), "no answer")
        return b.messages[-1]["text"]

    def test_connected_provider_answers_unknown_questions(self):
        p = Fake(CONNECTED)
        b = self.make(p)
        self.assertEqual(b.providerState, CONNECTED)
        self.assertEqual(self.ask(b, "tell me a joke"), "model says hi")
        self.assertEqual(p.prompts, ["tell me a joke"])

    def test_builtin_commands_never_hit_the_model(self):
        p = Fake(CONNECTED)
        b = self.make(p)
        self.assertIn("CPU", self.ask(b, "status"))
        self.assertEqual(p.prompts, [])

    def test_disconnected_provider_is_reported_truthfully(self):
        p = Fake(DISCONNECTED)
        b = self.make(p)
        self.assertEqual(b.providerState, DISCONNECTED)
        msg = self.ask(b, "tell me a joke")
        self.assertIn("unavailable", msg)
        self.assertEqual(p.prompts, [])

    def test_failure_surfaces_error_and_marks_provider_error(self):
        b = self.make(Fake(CONNECTED, exc=ProviderError("limit reached")))
        msg = self.ask(b, "tell me a joke")
        self.assertIn("limit reached", msg)
        self.assertTrue(pump(lambda: b.providerState == ERROR))

    def test_offline_default_says_no_model(self):
        b = self.make(None)
        self.assertEqual(b.providerState, "builtin")
        self.assertIn("no language model", self.ask(b, "tell me a joke"))

    def test_status_that_raises_does_not_crash(self):
        class Bad(Fake):
            def status(self):
                raise RuntimeError("x")
        b = self.make(Bad(CONNECTED))
        self.assertEqual(b.providerState, ERROR)


if __name__ == "__main__":
    unittest.main()

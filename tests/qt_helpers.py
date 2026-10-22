"""Shared helpers for tests that need a Qt event loop (offscreen, no window system needed)."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from jarvis.core.providers import ModelProvider, ProviderError, ProviderStatus  # noqa: E402


def gui_app():
    """One process-wide QGuiApplication (it must be a Gui app: the QML tests share it)."""
    return QGuiApplication.instance() or QGuiApplication([])


def pump(cond=None, timeout: float = 3.0, minimum: float = 0.0) -> bool:
    """Run the event loop until cond() is true (and at least `minimum` seconds passed).
    With no condition it simply runs the loop for `minimum` seconds."""
    start = time.time()
    end = start + (timeout if cond else minimum)
    while time.time() < end:
        QCoreApplication.processEvents()
        if cond and cond() and time.time() - start >= minimum:
            return True
        time.sleep(0.005)
    QCoreApplication.processEvents()
    return cond() if cond else True


class FakeProvider(ModelProvider):
    def __init__(self, name="fake", state="connected", detail="fake detail", answer="model says hi", exc=None):
        self.name, self.state, self.detail, self.answer, self.exc = name, state, detail, answer, exc
        self.prompts = []

    def status(self):
        return ProviderStatus(self.state, self.detail)

    def generate(self, prompt):
        self.prompts.append(prompt)
        if self.exc:
            raise ProviderError(self.exc)
        return self.answer

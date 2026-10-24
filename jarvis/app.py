"""Application entry point: `python -m jarvis`."""
import os
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from jarvis.core.providers import ModelProvider
from jarvis.core.store import Store
from jarvis.integrations.hotkey import GlobalHotkey
from jarvis.integrations.registry import make_provider
from jarvis.ui.backend import Backend

QML_DIR = Path(__file__).resolve().parent / "ui" / "qml"


def build(tasks_dir: Path | None = None, provider: ModelProvider | None = None,
          store: Store | None = None, system_control=None,
          audio_recorder=None, speech_recognizer=None,
          hotkey_factory=None) -> tuple[QQmlApplicationEngine, Backend]:
    """Create the QML engine and backend. Requires a QGuiApplication to exist.

    Nothing is persisted unless a Store is passed in; main() passes the real one."""
    QQuickStyle.setStyle("Basic")  # fully custom-styled controls
    store = store or Store(None)
    # JARVIS_PROVIDER overrides the saved preference for one launch
    provider = provider or make_provider(os.environ.get("JARVIS_PROVIDER") or store.get_pref("provider"))
    backend = (Backend(tasks_dir, provider, store=store, system_control=system_control,
                       audio_recorder=audio_recorder, speech_recognizer=speech_recognizer,
                       hotkey_factory=hotkey_factory)
               if tasks_dir else Backend(provider=provider, store=store, system_control=system_control,
                                         audio_recorder=audio_recorder, speech_recognizer=speech_recognizer,
                                         hotkey_factory=hotkey_factory))
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "Main.qml")))
    return engine, backend


def main(argv: list[str] | None = None) -> int:
    app = QGuiApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Jarvis")
    store = Store.open_default()
    engine, backend = build(store=store, hotkey_factory=GlobalHotkey)
    if not engine.rootObjects():
        print("Jarvis: failed to load the interface (see QML errors above).", file=sys.stderr)
        return 1
    app.aboutToQuit.connect(backend.shutdown)   # a request still running must not signal a dying object
    code = app.exec()
    store.close()
    return code

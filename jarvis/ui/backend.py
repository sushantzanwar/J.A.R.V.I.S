"""The single QObject exposed to QML as `backend`. Thin glue over jarvis.core.

Nothing here approves tasks, runs commands or touches files outside the local data store."""
import platform
import threading
from datetime import datetime
from pathlib import Path

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication

from jarvis import __version__
from jarvis.core import assistant, diagnostics
from jarvis.core.providers import CONNECTED, ERROR, PROVIDER_NAMES, ModelProvider, OfflineProvider, ProviderError
from jarvis.core.store import Store, now_iso
from jarvis.core.system_status import SystemMonitor
from jarvis.core.tasks import DEFAULT_TASKS_DIR, awaiting_approval, counts, read_tasks
from jarvis.core.worker_state import worker_state
from jarvis.integrations.registry import make_provider

MAX_INPUT = 500
GREETING = "Jarvis online. Type 'help' to see what I can do."
KIND_TEXT = {"app_started": "Jarvis started", "history_cleared": "Conversation history cleared",
             "prefs_reset": "Preferences reset to defaults"}


def fmt_time(ts: str) -> str:
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    return when.strftime("%H:%M") if when.date() == datetime.now().date() else when.strftime("%d %b %H:%M")


class Backend(QObject):
    statusChanged = Signal()
    tasksChanged = Signal()
    messagesChanged = Signal()
    orbModeChanged = Signal()
    providerChanged = Signal()
    prefsChanged = Signal()
    storeChanged = Signal()
    timelineChanged = Signal()
    workerChanged = Signal()
    _answerReady = Signal(str)                # emitted from a worker thread, delivered on the UI thread
    _providerChecked = Signal(str, str, str)  # provider name, state, detail

    def __init__(self, tasks_dir: Path = DEFAULT_TASKS_DIR, provider: ModelProvider | None = None, parent=None,
                 *, store: Store | None = None, factory=make_provider):
        super().__init__(parent)
        self._tasks_dir = Path(tasks_dir)
        self._bridge_dir = self._tasks_dir.parent
        self._worker_log = self._bridge_dir.parent / "logs" / "worker.jsonl"
        self._store = store or Store(None)  # no persistence unless a real store is passed in
        self._factory = factory
        self._provider = provider or OfflineProvider()
        self._monitor = SystemMonitor()
        self._status = self._monitor.snapshot()
        self._tasks = read_tasks(self._tasks_dir)
        self._known = {t["id"]: t["label"] for t in self._tasks}   # baseline: only later changes are logged
        self._worker = worker_state(self._bridge_dir, self._worker_log)
        self._orb = "idle"
        self._busy = False
        self._save_history = self._store.get_pref("save_history")
        self._reduce_motion = self._store.get_pref("reduce_motion")
        self._notice = self._store.notice
        history = [self._view(m) for m in self._store.messages()]
        self._messages = history or [self._greeting()]
        self._timeline: list[dict] = []

        self._answerReady.connect(self._deliver)
        self._providerChecked.connect(self._set_provider)
        self._pstate, self._pdetail = "checking", "checking provider…"
        self._record("app_started")
        self.refreshProvider()

        self._status_timer = QTimer(self, interval=2000)
        self._status_timer.timeout.connect(self.refreshStatus)
        self._tasks_timer = QTimer(self, interval=3000)
        self._tasks_timer.timeout.connect(self.refreshTasks)
        self._tasks_timer.timeout.connect(self.refreshWorker)
        QTimer.singleShot(400, self.refreshStatus)  # CPU load needs a second sample for a baseline
        self._status_timer.start()
        self._tasks_timer.start()

    # ---- helpers
    @staticmethod
    def _greeting() -> dict:
        return {"role": "jarvis", "text": GREETING, "time": fmt_time(now_iso())}

    @staticmethod
    def _view(m: dict) -> dict:
        return {"role": m["role"], "text": m["text"], "time": fmt_time(m["ts"])}

    def _sync_notice(self) -> None:
        if self._store.notice and self._store.notice != self._notice:
            self._notice = self._store.notice
            self.storeChanged.emit()

    def _record(self, kind: str, detail: str = "") -> None:
        self._store.add_activity(kind, detail)
        self._timeline = [{"time": fmt_time(a["ts"]), "kind": a["kind"],
                           "text": KIND_TEXT.get(a["kind"]) or a["detail"] or a["kind"].replace("_", " ")}
                          for a in self._store.activity(60)]
        self.timelineChanged.emit()
        self._sync_notice()

    def _append(self, role: str, text: str, persist: bool = True) -> None:
        if persist and self._save_history:
            self._store.add_message(role, text)
            self._sync_notice()
        self._messages = self._messages + [{"role": role, "text": text, "time": fmt_time(now_iso())}]
        self.messagesChanged.emit()

    def _explain(self) -> str:
        s, d = self._pstate, self._pdetail
        if s == "builtin":
            return ("Offline mode: only the built-in commands (help, status, tasks, time, about) are available. "
                    "Select Claude Code to use your signed-in Claude Code CLI.")
        if s == CONNECTED:
            return f"{d}."
        if s == "checking":
            return "Checking the provider…"
        if s == "disconnected":
            return f"{d}. Jarvis keeps working offline; fix this, then press Re-check."
        return f"{d}. Press Re-check to try again."

    # ---- properties for QML
    @Property("QVariantMap", notify=statusChanged)
    def status(self):
        return self._status

    @Property("QVariantList", notify=tasksChanged)
    def tasks(self):
        return self._tasks

    @Property(int, notify=tasksChanged)
    def awaitingCount(self):
        return len(awaiting_approval(self._tasks))

    @Property("QVariantMap", notify=tasksChanged)
    def taskCounts(self):
        return counts(self._tasks)

    @Property("QVariantList", notify=messagesChanged)
    def messages(self):
        return self._messages

    @Property(str, notify=orbModeChanged)
    def orbMode(self):
        return self._orb

    @Property(str, notify=providerChanged)
    def providerName(self):
        return self._provider.name

    @Property(str, notify=providerChanged)
    def providerState(self):
        return self._pstate

    @Property(str, notify=providerChanged)
    def providerDetail(self):
        return self._pdetail

    @Property(str, notify=providerChanged)
    def providerExplanation(self):
        return self._explain()

    @Property("QVariantList", constant=True)
    def providerNames(self):
        return list(PROVIDER_NAMES)

    @Property(bool, notify=prefsChanged)
    def saveHistory(self):
        return self._save_history

    @Property(bool, notify=prefsChanged)
    def reduceMotion(self):
        return self._reduce_motion

    @Property(str, notify=storeChanged)
    def storeNotice(self):
        return self._notice

    @Property(str, notify=storeChanged)
    def storeLocation(self):
        return str(self._store.path) if self._store.path else "In memory only (nothing is saved)"

    @Property(bool, notify=storeChanged)
    def storePersistent(self):
        return self._store.persistent

    @Property("QVariantList", notify=timelineChanged)
    def timeline(self):
        return self._timeline

    @Property("QVariantMap", notify=workerChanged)
    def workerState(self):
        return self._worker

    # ---- internal state changes
    def _set_orb(self, mode: str):
        if mode != self._orb:
            self._orb = mode
            self.orbModeChanged.emit()

    @Slot(str, str, str)
    def _set_provider(self, name: str, state: str, detail: str):
        if name != self._provider.name:   # a late answer from a provider that is no longer selected
            return
        self._pstate, self._pdetail = state, detail
        self.providerChanged.emit()

    # ---- refreshers
    @Slot()
    def refreshProvider(self):
        """Check the provider off the UI thread (it may start a process)."""
        provider = self._provider

        def check():
            try:
                st = provider.status()
                self._providerChecked.emit(provider.name, st.state, st.detail)
            except Exception as e:  # status() should never raise; stay truthful if it does
                self._providerChecked.emit(provider.name, ERROR, f"status check failed: {type(e).__name__}")
        threading.Thread(target=check, daemon=True).start()

    @Slot()
    def refreshStatus(self):
        self._status = self._monitor.snapshot()
        self.statusChanged.emit()

    @Slot()
    def refreshTasks(self):
        fresh = read_tasks(self._tasks_dir)
        if fresh == self._tasks:
            return
        for t in fresh:
            old = self._known.get(t["id"])
            if old is None:
                self._record("task_added", f"Task #{t['id']} added: {t['label']}")
            elif old != t["label"]:
                self._record("task_changed", f"Task #{t['id']}: {t['label']}")
        self._known = {t["id"]: t["label"] for t in fresh}
        self._tasks = fresh
        self.tasksChanged.emit()

    @Slot()
    def refreshWorker(self):
        fresh = worker_state(self._bridge_dir, self._worker_log)
        if fresh != self._worker:
            self._worker = fresh
            self.workerChanged.emit()

    # ---- settings (safe values only)
    @Slot(str)
    def setProvider(self, name: str):
        if name not in PROVIDER_NAMES or name == self._provider.name:
            return
        self._provider = self._factory(name)
        self._store.set_pref("provider", name)
        self._pstate, self._pdetail = "checking", "checking provider…"
        self.providerChanged.emit()
        self._record("provider_changed", f"Language model set to {name}")
        self.refreshProvider()

    @Slot(bool)
    def setSaveHistory(self, on: bool):
        self._save_history = bool(on)
        self._store.set_pref("save_history", self._save_history)
        self.prefsChanged.emit()
        self._record("pref_changed", "Saving conversation history " + ("on" if on else "off"))

    @Slot(bool)
    def setReduceMotion(self, on: bool):
        self._reduce_motion = bool(on)
        self._store.set_pref("reduce_motion", self._reduce_motion)
        self.prefsChanged.emit()

    @Slot()
    def resetPreferences(self):
        self._store.reset_prefs()
        self._save_history = self._store.get_pref("save_history")
        self._reduce_motion = self._store.get_pref("reduce_motion")
        self.prefsChanged.emit()
        self.setProvider("offline")
        self._record("prefs_reset")

    @Slot()
    def clearHistory(self):
        self._store.clear_messages()
        self._messages = [self._greeting()]
        self.messagesChanged.emit()
        self._record("history_cleared")

    @Slot()
    def dismissNotice(self):
        self._notice = ""
        self._store.notice = ""
        self.storeChanged.emit()

    # ---- diagnostics
    def _diag_info(self) -> dict:
        st = self._store
        c = st.counts()
        return {
            "version": __version__, "python": platform.python_version(), "pyside": PYSIDE_VERSION,
            "platform": platform.platform(),
            "provider": {"name": self._provider.name, "state": self._pstate, "detail": self._pdetail},
            "store": {"location": str(st.path) if st.path else "(memory)", "persistent": st.persistent,
                      "integrity": st.integrity(), "size_kb": st.size_bytes() / 1024, "messages": c["messages"],
                      "activity": c["activity"], "notice": st.notice or self._notice},
            "worker": self._worker, "tasks": counts(self._tasks),
        }

    @Slot(result=str)
    def diagnosticsText(self) -> str:
        return diagnostics.build(self._diag_info())

    @Slot()
    def copyDiagnostics(self):
        clipboard = QGuiApplication.clipboard() if QGuiApplication.instance() else None
        if clipboard:
            clipboard.setText(self.diagnosticsText())

    # ---- chat
    @Slot(str)
    def send(self, text: str):
        text = text.strip()[:MAX_INPUT]
        if not text:
            return
        self._append("user", text)
        if self._busy:
            self._append("jarvis", "Still working on your previous question.", persist=False)
            return
        builtin = assistant.answer(text, self._status, self._tasks)
        if builtin is not None:
            self._start(lambda: builtin, delay=450)
        elif self._pstate == CONNECTED:
            self._start(lambda: self._ask_model(text), delay=0)
        else:
            self._start(lambda: self._unavailable_message(), delay=450)

    def _unavailable_message(self) -> str:
        if self._provider.name == "offline":
            return assistant.FALLBACK
        return f"Language model unavailable ({self._pstate}): {self._pdetail}. Offline commands still work; type 'help'."

    def _ask_model(self, text: str) -> str:
        provider = self._provider
        try:
            return provider.generate(text)
        except ProviderError as e:
            self._providerChecked.emit(provider.name, ERROR, f"last request failed: {e}")
            return f"Language model error: {e}"
        except Exception as e:
            self._providerChecked.emit(provider.name, ERROR, f"last request failed: {type(e).__name__}")
            return f"Language model error: {type(e).__name__}"

    def _start(self, produce, delay: int):
        self._busy = True
        self._set_orb("thinking")

        def work():
            self._answerReady.emit(produce())

        if delay:
            ans = produce()
            QTimer.singleShot(delay, lambda: self._answerReady.emit(ans))
        else:
            threading.Thread(target=work, daemon=True).start()

    @Slot(str)
    def _deliver(self, answer: str):
        self._busy = False
        self._append("jarvis", answer)
        self._set_orb("speaking")
        QTimer.singleShot(1600, lambda: self._set_orb("idle"))

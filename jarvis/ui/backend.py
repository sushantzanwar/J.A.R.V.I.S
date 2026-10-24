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
from jarvis.core.context import ContextManager
from jarvis.core.providers import CONNECTED, ERROR, PROVIDER_NAMES, ModelProvider, OfflineProvider, ProviderError
from jarvis.core.store import Store, now_iso
from jarvis.core.system_status import SystemMonitor
from jarvis.core.tasks import DEFAULT_TASKS_DIR, awaiting_approval, counts, read_tasks
from jarvis.core.worker_state import worker_state
from jarvis.integrations.audio_recorder import AudioRecorder
from jarvis.integrations.registry import make_provider
from jarvis.integrations.speech_recognizer import SpeechRecognizer
from jarvis.integrations.system_control import SystemControl

MAX_INPUT = 500
GREETING = "Jarvis online. Type 'help' to see what I can do."
KIND_TEXT = {"app_started": "Jarvis started", "history_cleared": "Conversation history cleared",
             "prefs_reset": "Preferences reset to defaults", "tool_executed": "System tool action",
             "memory_saved": "Memory saved", "memory_updated": "Memory updated",
             "memory_archived": "Memory archived", "memory_forgotten": "Memory forgotten",
             "memories_cleared": "Memories cleared", "fts_rebuilt": "Search index rebuilt",
             "summary_created": "Conversation summary"}


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
    voiceRecordingChanged = Signal()
    voiceLevelChanged = Signal()
    memoriesChanged = Signal()
    _answerReady = Signal(str)                # emitted from a worker thread, delivered on the UI thread
    _providerChecked = Signal(str, str, str)  # provider name, state, detail
    _voiceTranscriptionReady = Signal(str, float)  # transcribed text, confidence

    def __init__(self, tasks_dir: Path = DEFAULT_TASKS_DIR, provider: ModelProvider | None = None, parent=None,
                 *, store: Store | None = None, factory=make_provider, system_control: SystemControl | None = None,
                 audio_recorder: AudioRecorder | None = None, speech_recognizer: SpeechRecognizer | None = None):
        super().__init__(parent)
        self._closing = False   # set by shutdown(): late results from worker threads are dropped
        self._tasks_dir = Path(tasks_dir)
        self._bridge_dir = self._tasks_dir.parent
        self._worker_log = self._bridge_dir.parent / "logs" / "worker.jsonl"
        self._store = store or Store(None)  # no persistence unless a real store is passed in
        self._factory = factory
        self._provider = provider or OfflineProvider()
        self._system_control = system_control or SystemControl()
        self._recorder = audio_recorder or AudioRecorder()
        self._recognizer = speech_recognizer or SpeechRecognizer()
        self._voice_recording = False
        self._voice_level = 0.0
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
        self._voiceTranscriptionReady.connect(self._on_voice_transcribed)
        self._pstate, self._pdetail = "checking", "checking provider…"
        self._record("app_started")
        self.refreshProvider()

        self._status_timer = QTimer(self, interval=2000)
        self._status_timer.timeout.connect(self.refreshStatus)
        self._tasks_timer = QTimer(self, interval=3000)
        self._tasks_timer.timeout.connect(self.refreshTasks)
        self._tasks_timer.timeout.connect(self.refreshWorker)
        self._voice_level_timer = QTimer(self, interval=80)
        self._voice_level_timer.timeout.connect(self._update_voice_level)
        QTimer.singleShot(400, self.refreshStatus)  # CPU load needs a second sample for a baseline
        self._status_timer.start()
        self._tasks_timer.start()

    def shutdown(self) -> None:
        """Stop timers and ignore anything background threads deliver from now on. Call before the application
        (or the engine that owns this object) goes away, so a request still running cannot signal a dead object."""
        self._closing = True
        self._status_timer.stop()
        self._tasks_timer.stop()
        self._voice_level_timer.stop()
        if self._voice_recording:
            self._recorder.stop()
            self._voice_recording = False

    def _emit_from_thread(self, signal, *args) -> None:
        if not self._closing:
            signal.emit(*args)

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

    @Property(bool, notify=voiceRecordingChanged)
    def voiceRecording(self):
        return self._voice_recording

    @Property(float, notify=voiceLevelChanged)
    def voiceLevel(self):
        return self._voice_level

    @Property("QVariantList", notify=memoriesChanged)
    def memories(self):
        return self._store.recall()

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
                self._emit_from_thread(self._providerChecked, provider.name, st.state, st.detail)
            except Exception as e:  # status() should never raise; stay truthful if it does
                self._emit_from_thread(self._providerChecked, provider.name, ERROR,
                                       f"status check failed: {type(e).__name__}")
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
                      "activity": c["activity"], "memories": c.get("memories", 0), "notice": st.notice or self._notice},
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

    # ---- memories (explicit user control)
    @Slot(str, str, str)
    def remember(self, key: str, value: str, category: str = "general"):
        res = self._store.remember(key, value, category=category)
        self.memoriesChanged.emit()
        return res

    @Slot(str, str)
    def forget(self, key: str, category: str = ""):
        res = self._store.forget(key, category=category or None)
        self.memoriesChanged.emit()
        return res

    @Slot(str, str)
    def archiveMemory(self, key: str, category: str = ""):
        res = self._store.archive(key, category=category or None)
        self.memoriesChanged.emit()
        return res

    @Slot(str)
    def clearMemories(self, category: str = ""):
        res = self._store.clear_memories(category=category or None, backup=True)
        self.memoriesChanged.emit()
        return res

    @Slot(result=str)
    def exportMemories(self) -> str:
        data = self._store.export_memories()
        clipboard = QGuiApplication.clipboard() if QGuiApplication.instance() else None
        if clipboard:
            clipboard.setText(data)
        return data

    @Slot(str, result="QVariantList")
    def searchMemories(self, query: str) -> list:
        return self._store.search_memories(query)

    @Slot(str, result="QVariantList")
    def searchMessages(self, query: str) -> list:
        return self._store.search_messages(query)

    @Slot(result="QVariantList")
    def summaries(self) -> list:
        return self._store.summaries()

    @Slot(result=str)
    def rebuildFts(self) -> str:
        res = self._store.rebuild_fts()
        return f"Rebuilt FTS: {res.get('memories_indexed', 0)} memories, {res.get('messages_indexed', 0)} messages"

    @Slot(result=str)
    def summarizeConversation(self) -> str:
        sumry = ContextManager.summarize_conversation(self._store)
        if sumry:
            self._record("summary_created", sumry["summary"])
            return sumry["summary"]
        return "No conversation history to summarize."

    @Slot(str, result="QVariantMap")
    def formatContext(self, query: str) -> dict:
        return ContextManager.format_context(self._store, query)

    @Slot(int, result=bool)
    def deleteSummary(self, summary_id: int) -> bool:
        res = self._store.delete_summary(summary_id)
        if res:
            self._record("summary_deleted", f"Deleted summary #{summary_id}")
        return res

    @Slot(result="QVariantMap")
    def ftsStats(self) -> dict:
        counts = self._store.counts()
        return {
            "memories_fts": counts.get("memories_fts", 0),
            "messages_fts": counts.get("messages_fts", 0),
            "last_rebuild_ts": counts.get("last_rebuild_ts") or "never",
            "last_search_latency_ms": counts.get("last_search_latency_ms", 0.0),
            "max_messages_retention": counts.get("max_messages_retention", 1000),
        }

    @Slot(result="QVariantMap")
    def memoryCountsByCategory(self) -> dict:
        mems = self._store.recall(status="active", limit=5000)
        c = {}
        for m in mems:
            cat = m["category"]
            c[cat] = c.get(cat, 0) + 1
        return c

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
        builtin = assistant.answer(text, self._status, self._tasks, system_control=self._system_control, store=self._store)
        if builtin is not None:
            if any(k in builtin for k in ("Volume", "Media", "Opened", "Workstation", "uptime:", "Battery", "Active window", "Skipped", "Returned", "remember", "memory", "forgotten", "Cleared", "Rebuilt", "Summary", "search")):
                self._record("tool_executed", builtin)
                self.memoriesChanged.emit()
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
            self._emit_from_thread(self._providerChecked, provider.name, ERROR, f"last request failed: {e}")
            return f"Language model error: {e}"
        except Exception as e:
            self._emit_from_thread(self._providerChecked, provider.name, ERROR,
                                   f"last request failed: {type(e).__name__}")
            return f"Language model error: {type(e).__name__}"

    def _start(self, produce, delay: int):
        self._busy = True
        self._set_orb("thinking")

        def work():
            self._emit_from_thread(self._answerReady, produce())

        if delay:
            ans = produce()
            QTimer.singleShot(delay, lambda: self._emit_from_thread(self._answerReady, ans))
        else:
            threading.Thread(target=work, daemon=True).start()

    @Slot(str)
    def _deliver(self, answer: str):
        self._busy = False
        self._append("jarvis", answer)
        self._set_orb("speaking")
        QTimer.singleShot(1600, lambda: self._set_orb("idle"))

    # ---- voice (Push-to-Talk)
    def _update_voice_level(self):
        if self._voice_recording:
            level = self._recorder.current_level
            if abs(level - self._voice_level) > 0.01:
                self._voice_level = level
                self.voiceLevelChanged.emit()

    @Slot()
    def startVoiceRecording(self):
        if self._busy or self._voice_recording:
            return
        if not self._recorder.start():
            return
        self._voice_recording = True
        self.voiceRecordingChanged.emit()
        self._set_orb("listening")
        self._voice_level_timer.start()

    @Slot()
    def stopVoiceRecording(self):
        if not self._voice_recording:
            return
        self._voice_recording = False
        self._voice_level_timer.stop()
        self._voice_level = 0.0
        self.voiceLevelChanged.emit()
        self.voiceRecordingChanged.emit()
        self._set_orb("thinking")

        recorder = self._recorder
        recognizer = self._recognizer

        def transcribe_task():
            import tempfile
            pcm = recorder.stop()
            if not pcm or len(pcm) < 3200:  # < 100ms of audio
                self._emit_from_thread(self._voiceTranscriptionReady, "", 0.0)
                return
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                tmp_path = Path(tf.name)
            try:
                recorder.save_wav(tmp_path, pcm)
                text, conf = recognizer.transcribe_wav(tmp_path)
            finally:
                if tmp_path.is_file():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
            self._emit_from_thread(self._voiceTranscriptionReady, text, conf)

        threading.Thread(target=transcribe_task, daemon=True).start()

    @Slot()
    def toggleVoiceRecording(self):
        if self._voice_recording:
            self.stopVoiceRecording()
        else:
            self.startVoiceRecording()

    @Slot(str, float)
    def _on_voice_transcribed(self, text: str, conf: float):
        if self._closing:
            return
        if text:
            self._record("tool_executed", f"Voice recognized: '{text}' (conf {conf:.2f})")
            self.send(text)
        else:
            self._append("jarvis", "I didn't catch that. Please speak clearly or type your request.", persist=False)
            self._set_orb("idle")


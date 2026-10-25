"""Append-only log of every action the agent takes, for audit.

One JSON object per line, secrets redacted before writing. Logging never raises: an audit
log that can crash the assistant, or be skipped by an exception, is worse than none.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from jarvis.core.redact import redact

MAX_BYTES = 5 * 1024 * 1024         # rotate to .1 beyond this
FIELD_LIMIT = 300


class ActionLog:
    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            from jarvis.core.store import default_data_dir
            path = default_data_dir() / "actions.jsonl"
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, *, tool: str, args: dict, decision: str, outcome: str, task_id: int | None = None,
               detail: str = "") -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "task": task_id, "tool": tool,
            "args": {str(k): redact(str(v))[:FIELD_LIMIT] for k, v in (args or {}).items()},
            "decision": decision, "outcome": outcome, "detail": redact(detail)[:FIELD_LIMIT],
        }
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.is_file() and self.path.stat().st_size > MAX_BYTES:
                    self.path.replace(self.path.with_suffix(".jsonl.1"))
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def tail(self, n: int = 20) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

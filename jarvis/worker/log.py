"""Structured (JSON lines) worker log with size-based rotation. Values are redacted before writing."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge"))
import bridge  # noqa: E402  (only redact() is used)

DEFAULT_LOG = ROOT / "logs" / "worker.jsonl"


class NullLog:
    def event(self, name: str, **fields) -> None:
        pass

    def tail(self, n: int = 5) -> list[dict]:
        return []


class JsonLog(NullLog):
    def __init__(self, path: Path = DEFAULT_LOG, max_bytes: int = 1_000_000, keep: int = 3):
        self.path, self.max_bytes, self.keep = Path(path), max_bytes, keep

    def _rotate(self) -> None:
        if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            for i in range(self.keep, 0, -1):
                src = self.path.with_name(f"{self.path.name}.{i - 1}") if i > 1 else self.path
                if src.exists():
                    src.replace(self.path.with_name(f"{self.path.name}.{i}"))

    def event(self, name: str, **fields) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": name}
        rec.update({k: bridge.redact(v) if isinstance(v, str) else v for k, v in fields.items()})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass  # logging must never break task processing

    def tail(self, n: int = 5) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
            return [json.loads(x) for x in lines]
        except (OSError, ValueError):
            return []

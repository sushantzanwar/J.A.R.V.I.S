"""Which actions may run unasked, which need a spoken "yes", and which never run.

Every tool declares a risk:
  low            runs immediately
  consequential  affects other people, other accounts or data that is hard to get back
                 (sending email or messages, creating or deleting calendar events and files)

The owner has chosen unrestricted control of this PC, so local PC actions, including power
actions, are low risk. Actions that reach outside the machine are consequential and ask first.
A user file (permissions.json in the data dir) overrides any tool: "allow", "confirm" or "deny".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ALLOW, CONFIRM, DENY = "allow", "confirm", "deny"
LEVELS = (ALLOW, CONFIRM, DENY)
RISK_DEFAULT = {"low": ALLOW, "consequential": CONFIRM}


@dataclass(frozen=True)
class Decision:
    level: str
    reason: str = ""


class Policy:
    def __init__(self, overrides: dict | None = None) -> None:
        self.overrides = {k: v for k, v in (overrides or {}).items() if v in LEVELS}

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Policy":
        if path is None:
            from jarvis.core.store import default_data_dir
            path = default_data_dir() / "permissions.json"
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(data.get("tools", data) if isinstance(data, dict) else {})
        except (OSError, ValueError):
            return cls()

    def decide(self, tool) -> Decision:
        override = self.overrides.get(tool.name)
        if override:
            return Decision(override, f"set to '{override}' in permissions.json")
        level = RISK_DEFAULT.get(getattr(tool, "risk", "low"), CONFIRM)     # unknown risk: ask
        return Decision(level, f"{getattr(tool, 'risk', 'unknown')} risk")

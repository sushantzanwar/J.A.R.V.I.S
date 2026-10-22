"""Read-only view of the background worker, derived from its pause file and structured log.

It never starts, stops or configures the worker; it only reports what the files say."""
import json
from datetime import datetime
from pathlib import Path

STALE_AFTER = 25 * 60  # scheduler fires every 10 min; no pass for 25 min means it is probably not running
TAIL_BYTES = 64 * 1024


def _tail_events(log_path: Path) -> list[dict]:
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-400:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def worker_state(bridge_dir: Path, log_path: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    paused = (Path(bridge_dir) / "PAUSE").exists()
    events = _tail_events(Path(log_path))
    passes = [e for e in events if e.get("event") in ("pass", "worker_start") and "ts" in e]
    last_pass = last_result = None
    age = None
    if passes:
        try:
            when = datetime.fromisoformat(passes[-1]["ts"])
            age = max(0, int((now - when).total_seconds()))
            last_pass = passes[-1]["ts"]
        except ValueError:
            pass
        results = [e for e in events if e.get("event") == "pass"]
        last_result = results[-1].get("result") if results else None
    if paused:
        state, detail = "paused", "Paused: the PAUSE file exists, so no tasks are processed. Remove it with: python -m jarvis.worker --resume"
    elif last_pass is None:
        state, detail = "unknown", "No worker activity recorded yet (the scheduled task may not be installed)."
    elif age is not None and age > STALE_AFTER:
        state, detail = "stale", f"No worker pass for {age // 60} min; the scheduled task may be disabled or not running."
    else:
        state, detail = "active", f"Last worker pass at {last_pass.replace('T', ' ')} ({(age or 0) // 60} min ago)."
    return {"state": state, "paused": paused, "last_pass": last_pass or "", "age_seconds": age if age is not None else -1,
            "last_result": last_result or "", "detail": detail}

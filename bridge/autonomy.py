"""Versioned policy for unattended tasks submitted through the local MCP bridge.

The policy is deliberately narrow: it can approve only local MCP tasks and never
commands-tier work.  The worker remains the enforcement point for execution and
the command allowlist remains the enforcement point for Bash.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POLICY_FILE = ROOT / "autonomy_policy.json"
DEFAULT = {
    "version": 1,
    "enabled": True,
    "trusted_sources": ["mcp"],
    "auto_approve_needs": ["read-only", "file-changes"],
    "max_followups": 2,
    "pause_file": "PAUSE",
}
SAFE_NEEDS = {"read-only", "file-changes"}


def load(path: Path = POLICY_FILE) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {**DEFAULT, "enabled": False}
    if not isinstance(data, dict) or data.get("version") != 1:
        return {**DEFAULT, "enabled": False}
    out = {**DEFAULT, **data}
    if not isinstance(out["trusted_sources"], list) or not isinstance(out["auto_approve_needs"], list) \
            or not isinstance(out["max_followups"], int) or out["max_followups"] < 0:
        return {**DEFAULT, "enabled": False}
    return out


def permits(source: str, needs: str, tasks_dir: Path, policy: dict | None = None) -> bool:
    policy = policy or load()
    if not policy.get("enabled") or (Path(tasks_dir).parent / policy["pause_file"]).exists():
        return False
    return source in policy["trusted_sources"] and needs in SAFE_NEEDS and needs in policy["auto_approve_needs"]

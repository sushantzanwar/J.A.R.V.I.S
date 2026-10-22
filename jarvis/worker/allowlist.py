"""Command allowlist for `commands` tasks. Default-deny: no file means no commands.

The file (bridge/command_allowlist.json) is written by a human and holds Claude Code permission rules of the
form  Bash(<exact command prefix> *). The worker refuses to load a list containing anything that could approve
tasks, change the scheduler, reach the network, install packages, delete data, touch secrets, or chain commands.
"""
import json
import re
from pathlib import Path

FORBIDDEN = re.compile(
    r"admin\.py|approve|schtasks|scheduledtask|jarvis[.\\/ ]worker|pip\s+install|git\s+(push|remote|config)|curl|wget|"
    r"invoke-(webrequest|restmethod|expression)|\biex\b|remove-item|\brm\b|\bdel\b|rmdir|\bformat\b|\breg\b|\bnet\b|"
    r"runas|sudo|-enc\b|executionpolicy|\.env|secret|token|credential|password|\.ssh|[;&|<>`$]",
    re.IGNORECASE)
RULE = re.compile(r"^Bash\((.+)\)$")


class AllowlistError(Exception):
    pass


def validate(rules) -> list[str]:
    if not isinstance(rules, list) or not rules:
        raise AllowlistError("allowlist must be a non-empty list of rules")
    out = []
    for r in rules:
        m = RULE.match(r) if isinstance(r, str) else None
        if not m:
            raise AllowlistError(f"rule must look like Bash(<command> *): {r!r}")
        inner = m.group(1).strip()
        if inner.startswith("*") or len(inner.replace("*", "").strip()) < 3:
            raise AllowlistError(f"rule is too broad: {r!r}")
        if FORBIDDEN.search(inner):
            raise AllowlistError(f"rule is not permitted: {r!r}")
        out.append(r)
    return out


def load(path: Path) -> list[str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AllowlistError(f"no allowlist at {path}") from None
    except (OSError, ValueError) as e:
        raise AllowlistError(f"unreadable allowlist: {type(e).__name__}") from None
    return validate(data.get("allow") if isinstance(data, dict) else None)

"""Scrub secret-looking strings before anything is stored or shown. Same patterns as bridge.redact."""
import re

_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"(?i:(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*)['\"]?[^\s'\"]{6,})"
)


def redact(text: str) -> str:
    return _SECRET.sub("[REDACTED]", text)

"""Claude Code CLI provider, using only the CLI's documented non-interactive mode (`claude -p`).

It runs under the user's existing `claude` login (e.g. a Pro plan); Jarvis never reads, stores or
forwards credentials. Every call goes through the first-party CLI, which enforces its own permission
system: tools are an explicit allowlist and anything else is denied (`--permission-mode dontAsk`).
Prompts travel on stdin; the argv is a fixed list and no shell is involved.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from jarvis.core.providers import (CONNECTED, DISCONNECTED, ERROR, ModelProvider, ProviderError,
                                   ProviderStatus)
from jarvis.integrations.process import run_contained

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHAT_SYSTEM_PROMPT = ("You are Jarvis, a concise assistant inside a desktop app. "
                      "You have no tools; answer from your own knowledge in a few sentences.")
UNSAFE_ARG = re.compile(r'[&|<>^%"\r\n]')
MAX_ERROR = 300


def resolve_exe() -> str | None:
    """Prefer the native binary behind npm's claude.cmd shim so no cmd.exe parsing is involved."""
    found = shutil.which("claude")
    if not found:
        return None
    if found.lower().endswith((".cmd", ".bat")):
        native = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.is_file():
            return str(native)
    return found


def clean_env(base=None) -> dict:
    """Child environment without API keys (so a subscription login is always used, never a paid API
    key) and without the parent Claude Code session's variables."""
    env = dict(os.environ if base is None else base)
    for k in list(env):
        if k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_PID", "CLAUDE_AGENT_SDK_VERSION") \
                or k.startswith(("CLAUDE_CODE_", "CLAUDECODE")):
            del env[k]
    return env


class ClaudeCodeProvider(ModelProvider):
    name = "claude-code"

    def __init__(self, exe: str | None = None, cwd: Path = PROJECT_ROOT, timeout: int = 180,
                 runner=run_contained):
        self._exe = exe
        self._cwd = Path(cwd)
        self._timeout = timeout
        self._run = runner

    def _exe_path(self) -> str | None:
        return self._exe or resolve_exe()

    def status(self) -> ProviderStatus:
        exe = self._exe_path()
        if not exe:
            return ProviderStatus(DISCONNECTED, "Claude Code CLI not found on PATH")
        try:
            p = self._run([exe, "auth", "status", "--json"], capture_output=True, text=True,
                          encoding="utf-8", timeout=30, env=clean_env(), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return ProviderStatus(ERROR, "Claude Code CLI did not answer the login check in time")
        except Exception as e:  # never raise from status()
            return ProviderStatus(ERROR, f"could not run Claude Code CLI: {type(e).__name__}")
        try:
            info = json.loads(p.stdout or "{}")
        except ValueError:
            info = {}
        if not info.get("loggedIn"):
            return ProviderStatus(DISCONNECTED, "Claude Code is not signed in (run: claude auth login)")
        # only non-identifying fields are surfaced; email/org ids are never read out
        method = info.get("authMethod", "unknown")
        return ProviderStatus(CONNECTED, f"Claude Code CLI signed in ({method}); usage counts against your plan")

    def build_argv(self, exe: str, tools=(), disallowed=(), allowed=None) -> list[str]:
        argv = [exe, "-p", "--output-format", "json", "--no-session-persistence",
                "--permission-mode", "dontAsk", "--tools", ",".join(tools)]
        allowed = tuple(tools) if allowed is None else tuple(allowed)
        if allowed:
            argv += ["--allowedTools", ",".join(allowed)]
        if disallowed:
            argv += ["--disallowedTools", ",".join(disallowed)]
        return argv

    def run(self, prompt: str, tools=(), disallowed=(), system_prompt: str | None = None, allowed=None) -> str:
        exe = self._exe_path()
        if not exe:
            raise ProviderError("Claude Code CLI not found on PATH")
        argv = self.build_argv(exe, tools, disallowed, allowed)
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        if any(UNSAFE_ARG.search(a) for a in argv[1:]):
            raise ProviderError("refusing to run: unsafe characters in command arguments")
        try:
            p = self._run(argv, input=prompt, capture_output=True, text=True, encoding="utf-8",
                          timeout=self._timeout, cwd=str(self._cwd), env=clean_env())
        except subprocess.TimeoutExpired:
            raise ProviderError(f"Claude Code did not finish within {self._timeout}s") from None
        except OSError as e:
            raise ProviderError(f"could not start Claude Code: {type(e).__name__}") from None
        try:
            data = json.loads(p.stdout)
        except ValueError:
            data = None
        if p.returncode != 0 or not isinstance(data, dict) or data.get("is_error"):
            msg = (data.get("result") if isinstance(data, dict) else None) or p.stderr or p.stdout or "no output"
            raise ProviderError(("Claude Code failed: " + str(msg).strip())[:MAX_ERROR])
        return str(data.get("result", "")).strip()

    def run_task(self, prompt: str, tools, disallowed=(), allowed=None) -> str:
        return self.run(prompt, tools=tuple(tools), disallowed=tuple(disallowed),
                        allowed=None if allowed is None else tuple(allowed))

    def in_workdir(self, path):
        return ClaudeCodeProvider(exe=self._exe, cwd=Path(path), timeout=self._timeout, runner=self._run)

    def generate(self, prompt: str) -> str:
        return self.run(prompt, tools=(), system_prompt=CHAT_SYSTEM_PROMPT)

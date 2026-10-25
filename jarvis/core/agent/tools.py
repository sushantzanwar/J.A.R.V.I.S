"""The tools the agent may use, and nothing else.

A plan can only name a tool registered here; anything else is rejected before execution,
so the agent cannot invent an action. Tool names match the SystemControl methods they
wrap, which lets the deterministic command grammar (SystemControl.resolve) and the
LLM planner produce identical steps.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote_plus

# Verification is the integration's own reported outcome. Jarvis cannot see the screen,
# so "Opened youtube" means Windows accepted the launch, not that a page finished loading.
# Any of these openings means the step did not do what was asked.
FAILURE_PREFIXES = ("could not", "i could not", "unknown tool", "nothing to open", "invalid",
                    "language model error", "language model unavailable", "error", "failed",
                    "that looked like a command line", "no built-in command matched")

MAX_WAIT_SECONDS = 30


@dataclass
class Tool:
    name: str
    description: str
    func: Callable[..., str]
    args: dict[str, str] = field(default_factory=dict)   # name -> description; all required
    kind: str = "action"        # action | query | power
    retryable: bool = True      # power and session actions are never repeated automatically


@dataclass
class ToolResult:
    ok: bool
    output: str


def verify(output: str) -> bool:
    text = (output or "").strip().lower()
    return bool(text) and not text.startswith(FAILURE_PREFIXES)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def describe(self) -> str:
        lines = []
        for t in self._tools.values():
            sig = ", ".join(t.args)
            lines.append(f"- {t.name}({sig}): {t.description}")
        return "\n".join(lines)

    def run(self, name: str, args: dict) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, f"Unknown tool '{name}'.")
        missing = sorted(a for a in tool.args if a not in args)
        extra = sorted(a for a in args if a not in tool.args)
        if missing or extra:
            return ToolResult(False, f"Invalid arguments for {name}: missing {missing}, unexpected {extra}.")
        try:
            out = tool.func(**{k: str(v) for k, v in args.items()})
        except Exception as e:      # a tool failure is a step failure, never a crash
            return ToolResult(False, f"Error in {name}: {type(e).__name__}: {e}")
        out = "" if out is None else str(out)
        return ToolResult(verify(out), out)


def _make_wait(sleep: Callable[[float], None]) -> Callable[[str], str]:
    def wait(seconds: str) -> str:
        try:
            s = float(seconds)
        except (TypeError, ValueError):
            return f"Invalid wait time '{seconds}'."
        s = max(0.0, min(s, MAX_WAIT_SECONDS))
        sleep(s)
        return f"Waited {s:g} seconds."
    return wait


def build_registry(system_control, *, local_command: Callable[[str], str] | None = None,
                   ask_model: Callable[[str], str] | None = None,
                   sleep: Callable[[float], None] = time.sleep) -> ToolRegistry:
    sc = system_control
    r = ToolRegistry()
    r.register(Tool("open_target", "Open an app, website or file, e.g. 'youtube', 'notepad', 'https://...'.",
                    sc.open_target, {"name": "app, website or URL"}))
    r.register(Tool("search_web", "Search the web in the browser.",
                    lambda query: sc.open_target("https://www.google.com/search?q=" + quote_plus(query)),
                    {"query": "what to search for"}))
    for name, desc in (("volume_up", "Raise the volume."), ("volume_down", "Lower the volume."),
                       ("volume_mute", "Mute or unmute the sound."), ("media_play_pause", "Play or pause media."),
                       ("media_next", "Skip to the next track."), ("media_prev", "Go to the previous track."),
                       ("media_stop", "Stop media playback.")):
        r.register(Tool(name, desc, getattr(sc, name)))
    for name, desc in (("get_battery", "Report the battery level."), ("get_uptime", "Report system uptime."),
                       ("get_active_window", "Name the window that has focus.")):
        r.register(Tool(name, desc, getattr(sc, name), kind="query"))
    r.register(Tool("cancel_power_action", "Cancel a pending shutdown or restart.", sc.cancel_power_action))
    # Power and session actions end what can usefully follow, so the planner runs them
    # last, and none is ever retried automatically.
    for name, desc in (("lock_workstation", "Lock the PC."), ("sleep_pc", "Put the PC to sleep."),
                       ("sign_out", "Sign out of Windows."), ("restart", "Restart the PC."),
                       ("shutdown", "Shut down the PC.")):
        r.register(Tool(name, desc, getattr(sc, name), kind="power", retryable=False))
    r.register(Tool("wait", f"Pause between steps, at most {MAX_WAIT_SECONDS} seconds.", _make_wait(sleep),
                    {"seconds": "number of seconds"}))
    if local_command is not None:
        r.register(Tool("local_command", "Run a built-in Jarvis command such as 'status', 'what time is it', "
                        "'tasks' or 'remember my editor is vscode'.", local_command, {"text": "the command"}))
    if ask_model is not None:
        r.register(Tool("ask_model", "Answer a question or write text with the language model.",
                        ask_model, {"question": "the question or request"}, kind="query"))
    return r

"""Turn a request into ordered, validated steps.

Two planners, cheapest first:
- split_compound: "open youtube and then mute" becomes steps with no model at all, using
  the same grammar as direct commands. Every part must resolve or nothing is split, so a
  sentence like "the difference between cats and dogs" is never chopped up.
- LLMPlanner: asks a model for a JSON plan. Its output is untrusted: validate_plan rejects
  unknown tools, wrong argument names, and dependencies on missing or later steps.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Callable

from jarvis.core.agent.tools import ToolRegistry

MAX_STEPS = 8
UNSUPPORTED: list = []   # parts of the last plan the model said it cannot do


class PlanError(ValueError):
    """The planner's output cannot be executed as given."""


class Infeasible(PlanError):
    """The planner says the goal cannot be done with the available tools. Not retried."""


@dataclass
class Step:
    id: int
    tool: str
    args: dict = field(default_factory=dict)
    depends_on: list = field(default_factory=list)
    success: str = ""
    status: str = "pending"     # pending running done failed skipped replaced unknown
    attempts: int = 0
    output: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# Verbs no registered tool can perform. A 2B planner will not reliably admit this itself, so
# the check is deterministic: if the request asks for one, the summary says it was NOT done.
UNSUPPORTED_VERB = re.compile(
    r"\b(type|send|email|e-mail|click|double[- ]click|press|call|text|message|download|upload|install|uninstall|"
    r"delete|remove|copy|paste|save|print|screenshot|record|book|buy|order|pay|post|tweet|log\s+in|sign\s+in)\b"
    r"[^,;.]*", re.IGNORECASE)


def unsupported_parts(goal: str) -> list[str]:
    """Parts of the request that need an ability the agent does not have."""
    out = []
    for m in UNSUPPORTED_VERB.finditer(goal):
        part = " ".join(m.group(0).split())
        part = re.split(r"\s+(?:and|then|after)\s+", part, 1, flags=re.IGNORECASE)[0]
        if part and part.lower() not in (p.lower() for p in out):
            out.append(part[:60])
    return out[:4]


def extract_json(text: str) -> dict:
    """The first JSON object in a model reply, tolerating prose or code fences around it."""
    if not text:
        raise PlanError("the planner returned nothing")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise PlanError("the planner did not return JSON")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise PlanError(f"the planner returned malformed JSON ({e.msg})") from None
    if not isinstance(data, dict):
        raise PlanError("the planner's JSON is not an object")
    return data


# A model may only plan a power or session action that the request itself asks for.
# This is not a confirmation prompt: "shut down" still shuts down at once. It stops a
# small model from inventing one; qwen3.5:2b planned "restart" for "get my workspace
# ready for coding" during testing.
POWER_REQUESTS = {
    "shutdown": re.compile(r"\b(?:shut\s*down|power\s*off|turn\s+off\s+(?:the\s+|my\s+)?(?:pc|computer|laptop|system|machine))\b"),
    "restart": re.compile(r"\b(?:restart|reboot)\b"),
    "sleep_pc": re.compile(r"\b(?:sleep|hibernate|suspend)\b"),
    "sign_out": re.compile(r"\b(?:sign\s*out|log\s*(?:out|off))\b"),
    "lock_workstation": re.compile(r"\block\b"),
}


def requested(tool: str, goal: str, resolve) -> bool:
    """Did the request actually COMMAND this power action?

    With the command grammar available, a clause must resolve to exactly this tool, so "open sleep
    timer" or "why can't I sleep" never allow a planned sleep. Without it, fall back to the word check.
    """
    if resolve is not None:
        parts = [p.strip(" .!?") for p in _CONNECTOR.split(goal) if p.strip(" .!?")] or [goal]
        for part in [goal, *parts]:
            call = resolve(part)
            if call is not None and call[0] == tool:
                return True
        return False
    return bool(POWER_REQUESTS[tool].search(goal.lower()))


def _tool_name(raw) -> str:
    """Models write 'volume_mute()' or ' Volume_Mute '; the tool is still volume_mute."""
    return re.sub(r"\(.*\)\s*$", "", str(raw or "")).strip().lower()


def validate_plan(data: dict, registry: ToolRegistry, *, first_id: int = 1,
                  done_ids: tuple = (), goal: str | None = None, resolve=None) -> list[Step]:
    """Untrusted plan -> executable steps, renumbered from first_id. Raises PlanError."""
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        raise PlanError("the plan has no 'steps' list")
    if not raw_steps:
        raise Infeasible(str(data.get("reason") or "the planner found no way to do that with the available tools"))
    if len(raw_steps) > MAX_STEPS:
        raise PlanError(f"the plan has {len(raw_steps)} steps; the limit is {MAX_STEPS}")

    remap: dict = {}
    steps: list[Step] = []
    for i, raw in enumerate(raw_steps):
        n = i + 1
        if not isinstance(raw, dict):
            raise PlanError(f"step {n} is not an object")
        tool = _tool_name(raw.get("tool"))
        if tool not in registry:
            raise PlanError(f"step {n} uses '{tool}', which is not an available tool")
        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise PlanError(f"step {n} has arguments that are not an object")
        if goal is not None and tool in POWER_REQUESTS and not requested(tool, goal, resolve):
            raise PlanError(f"step {n} uses {tool}, but the request never asked for that; remove it")
        spec = registry.get(tool).args
        missing, extra = sorted(set(spec) - set(args)), sorted(set(args) - set(spec))
        if missing or extra:
            raise PlanError(f"step {n} ({tool}) has wrong arguments: missing {missing}, unexpected {extra}")
        new_id = first_id + i
        remap[raw.get("id", n)] = new_id
        deps = raw.get("depends_on") or []
        if not isinstance(deps, list):
            raise PlanError(f"step {n} has a 'depends_on' that is not a list")
        new_deps = []
        for d in deps:
            if d in remap and remap[d] < new_id:
                new_deps.append(remap[d])
            elif d in done_ids:
                new_deps.append(d)
            else:
                raise PlanError(f"step {n} depends on step {d}, which is missing or comes later")
        steps.append(Step(new_id, tool, {k: str(v) for k, v in args.items()}, new_deps,
                          str(raw.get("success", ""))[:200]))
    unsupported = data.get("unsupported") or []
    if isinstance(unsupported, list):
        UNSUPPORTED[:] = [str(u)[:80] for u in unsupported if str(u).strip()][:5]
    return order_power_last(steps, registry, first_id)


def order_power_last(steps: list[Step], registry: ToolRegistry, first_id: int = 1) -> list[Step]:
    """Shutdown, restart, sleep, sign-out and lock end the session, so they run after
    everything else. A step that depends on one of them could never run, so is rejected."""
    power_ids = {s.id for s in steps if registry.get(s.tool).kind == "power"}
    if not power_ids:
        return steps
    for s in steps:
        if set(s.depends_on) & power_ids:
            raise PlanError(f"step {s.id} depends on a power action, which must come last")
    ordered = [s for s in steps if s.id not in power_ids] + [s for s in steps if s.id in power_ids]
    renumber = {s.id: first_id + i for i, s in enumerate(ordered)}
    for s in ordered:
        s.depends_on = [renumber.get(d, d) for d in s.depends_on]
        s.id = renumber[s.id]
    return ordered


# ---- deterministic compound commands -------------------------------------------------

_CONNECTOR = re.compile(
    r"\s*(?:,\s*and\s+then|,\s*then|\band\s+then|\bafter\s+that|\bthen|,\s*and|\band|;|,)\s+",
    re.IGNORECASE)
_LAUNCH_VERBS = ("open", "launch", "start", "run", "go to")
# A bare part starting with one of these is its own (unsupported) action, so the previous
# verb must not be carried onto it: "open notepad and type hello" is not "open type hello".
_OWN_VERBS = {"type", "write", "send", "tell", "say", "find", "search", "close", "check", "read", "make",
              "create", "delete", "remove", "move", "copy", "show", "give", "set", "turn", "put", "take",
              "email", "call", "text", "save", "download", "upload", "install", "uninstall", "kill"}

Resolver = Callable[[str], "tuple[str, dict] | None"]


def split_compound(text: str, resolve: Resolver) -> list[Step] | None:
    """Steps for a compound command made only of known commands, else None."""
    parts = [p.strip(" .!?") for p in _CONNECTOR.split(text.strip()) if p and p.strip(" .!?")]
    if len(parts) < 2 or len(parts) > MAX_STEPS:
        return None
    steps: list[Step] = []
    carry = None
    for part in parts:
        call = resolve(part)
        words = part.split()
        if call is None and carry and words and words[0].lower() not in _OWN_VERBS and len(words) <= 4:
            call = resolve(f"{carry} {part}")
        if call is None:
            return None
        tool, args = call
        steps.append(Step(len(steps) + 1, tool, dict(args), [], ""))
        low = part.lower()
        if tool == "open_target":
            carry = next((v for v in _LAUNCH_VERBS if low.startswith(v + " ")), carry)
        else:
            carry = None
    return steps


# ---- model planner -------------------------------------------------------------------

PLANNER_SYSTEM = """You plan actions for JARVIS, an assistant that fully controls a Windows PC.
Reply with JSON only, in exactly this shape:
{"steps": [{"id": 1, "tool": "<tool name>", "args": {"<arg>": "<value>"}, "depends_on": [], "success": "<what success looks like>"}], "unsupported": []}
Rules:
- Use ONLY the tools listed, with exactly the argument names shown. Never invent a tool.
- Use as few steps as possible, at most 8. Include ONLY steps the goal asks for; never add extra actions.
- To search for something use search_web. open_target only opens a real app, website or file.
- depends_on lists ids of earlier steps that must succeed first; use [] when a step is independent.
- For a question, or for writing or explaining something, use ask_model.
- If the goal cannot be done with these tools, reply {"steps": [], "reason": "<why>"}.
- If only PART of the goal is possible (for example there is no tool to type text, send email or click),
  plan the possible part and list the rest in "unsupported": ["<part you cannot do>"]. Never substitute
  a different action, and never use ask_model to pretend an action was done."""


def build_prompt(goal: str, registry: ToolRegistry, context: str = "") -> str:
    ctx = f"{context.strip()}\n\n" if context else ""
    return f"Tools:\n{registry.describe()}\n\n{ctx}Goal: {goal}\nJSON plan:"


class LLMPlanner:
    def __init__(self, provider, name: str, resolve=None) -> None:
        self.provider = provider
        self.name = name
        self.resolve = resolve

    def plan(self, goal: str, registry: ToolRegistry, *, first_id: int = 1,
             done_ids: tuple = (), context: str = "") -> list[Step]:
        prompt = build_prompt(goal, registry, context)
        if getattr(self.provider, "supports_json", False):
            raw = self.provider.generate(prompt, system=PLANNER_SYSTEM, json_mode=True)
        else:
            raw = self.provider.generate(f"{PLANNER_SYSTEM}\n\n{prompt}")
        return validate_plan(extract_json(raw), registry, first_id=first_id, done_ids=done_ids, goal=goal, resolve=self.resolve)

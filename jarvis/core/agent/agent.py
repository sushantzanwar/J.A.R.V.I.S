"""The agent: understand a request, plan it, run it, report what actually happened.

Request understanding (deterministic, no model):
- control  "resume task", "task status"            -> handled here
- compound "open youtube and then mute"            -> split with the command grammar
- goal     an imperative the grammar can't resolve -> planned by a model
- simple commands and questions never reach the agent; Backend answers those as before.

Provider strategy: local logic first; local Ollama for planning; the Claude provider only
when the user has made it their active provider AND Ollama could not produce a valid plan.
"""
from __future__ import annotations

import re
import time
from typing import Callable

from jarvis.core.agent.executor import Executor, RunReport
from jarvis.core.agent import planner as _planner
from jarvis.core.agent.planner import (Infeasible, LLMPlanner, PlanError, Step, split_compound,
                                       unsupported_parts, _CONNECTOR)
from jarvis.core.agent.tools import build_registry
from jarvis.core.assistant import is_builtin_query
from jarvis.core.providers import CONNECTED, ProviderError

RESUME_RE = re.compile(
    r"^(?:please\s+)?(?:resume|continue)\s+(?:the\s+|my\s+)?(?:last\s+|previous\s+|interrupted\s+)?(?:task|plan|job)\b"
    r"|^(?:resume|continue)\s+where\s+(?:you|we)\s+left\s+off\b")
TASK_STATUS_RE = re.compile(
    r"^(?:task\s+status|agent\s+status|what\s+(?:are|were)\s+you\s+doing"
    r"|(?:show\s+(?:me\s+)?)?(?:the\s+)?last\s+task)\b")
GOAL_RE = re.compile(
    r"^(?:(?:please|jarvis|hey\s+jarvis|ok\s+jarvis|can\s+you|could\s+you|would\s+you)[,\s]+)*"
    r"(?:open|launch|start|run|play|close|set\s*up|prepare|get\s+(?:my|the|me|it|everything|ready)|make|turn|"
    r"switch|put|organi[sz]e|clean\s*up|shut|restart|reboot|lock|mute|unmute|increase|decrease|raise|lower|"
    r"pause|resume|search|find|look\s+up|check|go\s+to|navigate|show)\b")

PLAN_CORRECTIONS = 1    # extra attempts per planner after a rejected plan


class Agent:
    def __init__(self, system_control, *, store=None,
                 chat_provider: Callable[[], object] = lambda: None,
                 chat_connected: Callable[[], bool] = lambda: False,
                 ollama_factory: Callable[[], object] | None = None,
                 local_command: Callable[[str], str] | None = None,
                 progress: Callable[[str], None] | None = None,
                 planners: Callable[[], list] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.system_control = system_control
        self.store = store
        self._chat_provider = chat_provider
        self._chat_connected = chat_connected
        self._ollama_factory = ollama_factory
        self._ollama = None
        self._planners_override = planners
        self._progress = progress or (lambda msg: None)
        self.registry = build_registry(system_control, local_command=local_command,
                                       ask_model=self._ask_model, sleep=sleep)
        self.executor = Executor(self.registry, store=store, progress=self._emit)
        if store is not None:
            # Nothing is running at startup; anything left mid-flight was interrupted.
            store.mark_interrupted_agent_tasks()

    def _emit(self, msg: str) -> None:
        try:
            self._progress(msg)
        except Exception:
            pass

    # ---- request understanding -------------------------------------------------------
    def _resolve(self, part: str):
        call = self.system_control.resolve(part)
        if call is not None:
            return call
        if is_builtin_query(part) and "local_command" in self.registry:
            return "local_command", {"text": part}
        return None

    def compound_steps(self, text: str) -> list[Step] | None:
        return split_compound(text, self._resolve)

    def is_goal(self, text: str) -> bool:
        return bool(GOAL_RE.search(text.strip().lower()))

    def wants(self, text: str) -> bool:
        """Requests the agent must see BEFORE the single-command path, which would
        otherwise swallow "open youtube and search for cats" as one strange app name."""
        low = text.strip().lower()
        if RESUME_RE.search(low) or TASK_STATUS_RE.search(low):
            return True
        if self.compound_steps(text):
            return True
        return bool(_CONNECTOR.search(low) and GOAL_RE.search(low))

    # ---- providers ---------------------------------------------------------------------
    def _get_ollama(self):
        if self._ollama is None and self._ollama_factory is not None:
            try:
                self._ollama = self._ollama_factory()
            except Exception:
                self._ollama = None
        return self._ollama

    @staticmethod
    def _is_connected(provider) -> bool:
        try:
            return provider.status().state == CONNECTED
        except Exception:
            return False

    def planners(self) -> list[LLMPlanner]:
        if self._planners_override is not None:
            return list(self._planners_override())
        out = []
        ollama = self._get_ollama()
        if ollama is not None and getattr(ollama, "name", "") == "ollama" and self._is_connected(ollama):
            out.append(LLMPlanner(ollama, "ollama"))
        chat = self._chat_provider()
        # Claude costs plan usage, so it is used only when the user chose it as their provider,
        # and only after Ollama (listed first) failed to plan.
        if chat is not None and getattr(chat, "name", "") == "claude-code" and self._chat_connected():
            out.append(LLMPlanner(chat, "claude-code"))
        return out

    def _ask_model(self, question: str) -> str:
        chat = self._chat_provider()
        provider = chat if chat is not None and self._chat_connected() else None
        if provider is None or getattr(provider, "name", "") == "offline":
            ollama = self._get_ollama()
            provider = ollama if ollama is not None and self._is_connected(ollama) else None
        if provider is None:
            return "Language model unavailable: no connected model to answer with."
        try:
            return provider.generate(question)
        except ProviderError as e:
            return f"Language model error: {e}"

    def _plan(self, goal: str, *, first_id: int = 1, done_ids: tuple = (),
              context: str = "") -> tuple[list[Step], str]:
        planners = self.planners()
        if not planners:
            raise PlanError("no planning model is available (start Ollama, or select Claude Code)")
        errors = []
        for planner in planners:
            feedback = ""
            for _ in range(1 + PLAN_CORRECTIONS):
                try:
                    steps = planner.plan(goal, self.registry, first_id=first_id, done_ids=done_ids,
                                         context=context + feedback)
                    return steps, planner.name
                except Infeasible as e:
                    errors.append(f"{planner.name}: {e}")
                    break
                except PlanError as e:
                    errors.append(f"{planner.name}: {e}")
                    feedback = f"\nYour previous plan was rejected: {e}. Return a corrected JSON plan."
                except ProviderError as e:
                    errors.append(f"{planner.name}: {e}")
                    break
                except Exception as e:
                    errors.append(f"{planner.name}: {type(e).__name__}")
                    break
        raise PlanError("; ".join(errors))

    def _replanner(self, goal: str):
        def replan(failed: Step, done: list, remaining: list):
            known = [failed, *done, *remaining]
            context = ("Already completed: " + (", ".join(f"{s.tool} {s.args}" for s in done) or "nothing") + ".\n"
                       f"This step failed: {failed.tool} {failed.args} -> {failed.output}\n"
                       "Plan only the remaining work. Do not repeat completed steps.")
            steps, _ = self._plan(goal, first_id=max(s.id for s in known) + 1,
                                  done_ids=tuple(s.id for s in done), context=context)
            return steps
        return replan

    # ---- running -----------------------------------------------------------------------
    def handle(self, text: str) -> str:
        """Entry point, called on a worker thread. Always returns a truthful summary."""
        q = text.strip()
        low = q.lower()
        if RESUME_RE.search(low):
            return self.resume()
        if TASK_STATUS_RE.search(low):
            return self.status_report()
        steps = self.compound_steps(q)
        if steps:
            return self._run_new(q, steps, "local", replan=None)
        _planner.UNSUPPORTED.clear()
        try:
            steps, planner = self._plan(q)
        except Infeasible as e:
            return f"I can't do that with the tools I have: {e}"
        except PlanError as e:
            return f"I couldn't plan that: {e}"
        unsupported = unsupported_parts(q) or list(_planner.UNSUPPORTED)
        result = self._run_new(q, steps, planner, replan=self._replanner(q))
        if unsupported:
            result += " I could NOT do: " + "; ".join(unsupported) + " (no tool for that)."
        return result

    def _run_new(self, goal: str, steps: list[Step], planner: str, replan) -> str:
        task_id = self.store.create_agent_task(goal, planner) if self.store is not None else None
        if self.store is not None and task_id is not None:
            self.store.save_agent_steps(task_id, [s.as_dict() for s in steps])
        self._emit(f"Plan ({planner}): " + ", ".join(f"{s.id}. {s.tool}" for s in steps))
        report = self.executor.execute(task_id, steps, replan=replan)
        return self._final(report)

    @staticmethod
    def _final(report: RunReport) -> str:
        return f"Task #{report.task_id}: {report.summary}" if report.task_id else report.summary

    def resume(self) -> str:
        if self.store is None:
            return "Task history is not available in this session."
        tasks = self.store.agent_tasks(statuses=("interrupted",), limit=1)
        if not tasks:
            return "There is no interrupted task to resume."
        t = tasks[0]
        steps = [Step(**s) for s in t["steps"]]
        for s in steps:
            if s.status in ("unknown", "running"):
                tool = self.registry.get(s.tool)
                if tool is None or not tool.retryable:
                    # It may have run before the interruption; never fire it twice.
                    s.status, s.output = "skipped", "Not repeated after the interruption: it may already have run."
                else:
                    s.status = "pending"
        self.store.save_agent_steps(t["id"], [s.as_dict() for s in steps])
        self._emit(f"Resuming task #{t['id']}: {t['goal']}")
        replan = self._replanner(t["goal"]) if t["planner"] not in ("", "local") else None
        report = self.executor.execute(t["id"], steps, replan=replan)
        return f"Resumed task #{t['id']}. {report.summary}"

    def status_report(self) -> str:
        if self.store is None:
            return "Task history is not available in this session."
        tasks = self.store.agent_tasks(limit=1)
        if not tasks:
            return "No agent tasks yet."
        t = tasks[0]
        counts = {}
        for s in t["steps"]:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        detail = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
        return f"Last task #{t['id']} is {t['status']}: {t['goal']} ({detail}). {t['summary']}".strip()

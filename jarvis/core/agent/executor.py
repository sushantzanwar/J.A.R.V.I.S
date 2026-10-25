"""Run a plan one step at a time, honestly.

A step is marked done only after its tool actually ran and reported success; the summary
is built from those records, never from the plan. Failures get one automatic retry
(never for power actions), steps whose dependencies failed are skipped, and the planner
gets one chance to route around a failure before the task stops.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from jarvis.core.agent.planner import Step
from jarvis.core.agent.tools import ToolRegistry

MAX_RETRIES = 1     # automatic retries for a failed retryable step
MAX_REPLANS = 1     # attempts to plan around a failure

# replan(failed_step, done_steps, remaining_steps) -> replacement steps, or None
Replanner = Callable[[Step, list, list], "list[Step] | None"]


@dataclass
class RunReport:
    task_id: int | None
    status: str          # done | partial | failed | awaiting
    steps: list
    summary: str
    awaiting: object = None      # the step waiting for the user's yes/no, if any


def _short(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


class Executor:
    def __init__(self, registry: ToolRegistry, *, store=None,
                 progress: Callable[[str], None] | None = None, max_retries: int = MAX_RETRIES,
                 policy=None, actionlog=None) -> None:
        self.registry = registry
        self.store = store
        self.progress = progress or (lambda msg: None)
        self.max_retries = max_retries
        self.policy = policy          # jarvis.core.security.Policy, or None = everything allowed
        self.actionlog = actionlog    # jarvis.core.security.ActionLog, or None

    def _log(self, task_id, step: Step, decision: str, outcome: str, detail: str = "") -> None:
        if self.actionlog is not None:
            self.actionlog.record(tool=step.tool, args=step.args, decision=decision, outcome=outcome,
                                  task_id=task_id, detail=detail)

    # ---- persistence (optional: tests and private mode run without a store)
    def _save(self, task_id, steps) -> None:
        if self.store is not None and task_id is not None:
            self.store.save_agent_steps(task_id, [s.as_dict() for s in steps])

    def _update(self, task_id, step: Step) -> None:
        if self.store is not None and task_id is not None:
            self.store.update_agent_step(task_id, step.id, status=step.status,
                                         attempts=step.attempts, output=step.output)

    def execute(self, task_id: int | None, steps: list[Step], *, replan: Replanner | None = None,
                replans_left: int = MAX_REPLANS, approved: set | None = None) -> RunReport:
        steps = list(steps)
        approved = approved if approved is not None else set()
        awaiting = None
        if self.store is not None and task_id is not None:
            self.store.update_agent_task(task_id, status="running")
        total = len(steps)
        i = 0
        while i < len(steps):
            step = steps[i]
            i += 1
            if step.status != "pending":
                continue
            by_id = {s.id: s for s in steps}
            blocked = [d for d in step.depends_on if by_id.get(d) is None or by_id[d].status != "done"]
            if blocked:
                step.status = "skipped"
                step.output = f"Not run: it depends on step {', '.join(map(str, blocked))}, which did not succeed."
                self._update(task_id, step)
                self.progress(f"Step {step.id} skipped ({step.tool}): depends on a failed step.")
                continue

            tool = self.registry.get(step.tool)

            # Permissions come before anything runs. A model-written plan cannot talk its way past this.
            if self.policy is not None and tool is not None:
                verdict = self.policy.decide(tool)
                if verdict.level == "deny":
                    step.status = "failed"
                    step.output = f"Blocked by your permissions settings ({verdict.reason})."
                    self._update(task_id, step)
                    self._log(task_id, step, "deny", "blocked", verdict.reason)
                    self.progress(f"Step {step.id} blocked: {step.tool}")
                    continue
                if verdict.level == "confirm" and step.id not in approved:
                    step.status = "awaiting"
                    step.output = "Waiting for your confirmation."
                    self._update(task_id, step)
                    self._log(task_id, step, "confirm", "requested", verdict.reason)
                    awaiting = step
                    break
            decision = "approved" if (self.policy is not None and step.id in approved) else "allow"
            allowed = 1 + (self.max_retries if tool is not None and tool.retryable else 0)
            result = None
            while step.attempts < allowed:
                step.attempts += 1
                step.status = "running"
                self._update(task_id, step)      # persisted BEFORE acting: a crash leaves 'running'
                result = self.registry.run(step.tool, step.args)
                if result.ok:
                    break
            if result is None:                   # attempts were already used up before a resume
                step.status, step.output = "failed", step.output or "No attempts left."
            else:
                step.status = "done" if result.ok else "failed"
                step.output = result.output
            self._update(task_id, step)
            self._log(task_id, step, decision, "ok" if step.status == "done" else "failed", step.output)
            mark = "done" if step.status == "done" else "FAILED"
            self.progress(f"Step {step.id}/{max(total, len(steps))} {mark}: {step.tool} - {_short(step.output, 70)}")

            if step.status == "failed" and replan is not None and replans_left > 0:
                replans_left -= 1
                remaining = [s for s in steps[i:] if s.status == "pending"]
                done = [s for s in steps if s.status == "done"]
                try:
                    new_steps = replan(step, done, remaining)
                except Exception as e:          # a planner failure ends replanning, not the run
                    self.progress(f"Could not replan: {_short(str(e), 70)}")
                    new_steps = None
                if new_steps:
                    for s in remaining:
                        s.status, s.output = "replaced", f"Replaced by a new plan after step {step.id} failed."
                        self._update(task_id, s)
                    steps.extend(new_steps)
                    self._save(task_id, new_steps)
                    if self.store is not None and task_id is not None:
                        self.store.update_agent_task(task_id, replans=MAX_REPLANS - replans_left)
                    self.progress(f"Replanned around step {step.id}: {len(new_steps)} new step(s).")
        return self._report(task_id, steps, awaiting)

    def _report(self, task_id, steps, awaiting=None) -> RunReport:
        counted = [s for s in steps if s.status != "replaced"]
        done = [s for s in counted if s.status == "done"]
        failed = [s for s in counted if s.status == "failed"]
        skipped = [s for s in counted if s.status == "skipped"]
        if awaiting is not None:
            status = "awaiting"
        elif not failed and not skipped:
            status = "done"
        elif done:
            status = "partial"
        else:
            status = "failed"
        parts = [f"{len(done)} of {len(counted)} step(s) succeeded."]
        if done:
            parts.append("Done: " + "; ".join(_short(s.output, 60) for s in done) + ".")
        if failed:
            parts.append("Failed: " + "; ".join(f"{s.tool} ({_short(s.output, 60)})" for s in failed) + ".")
        if skipped:
            parts.append(f"Skipped {len(skipped)} step(s) that depended on a failure"
                         f"{' or may already have run' if any('may already' in s.output for s in skipped) else ''}.")
        if awaiting is not None:
            parts.append("Waiting for your confirmation to " + self.registry.describe_call(awaiting.tool, awaiting.args) + ".")
        summary = " ".join(parts)
        if self.store is not None and task_id is not None:
            self.store.update_agent_task(task_id, status="awaiting_confirmation" if awaiting is not None else status,
                                         summary=summary)
        return RunReport(task_id, status, steps, summary, awaiting)

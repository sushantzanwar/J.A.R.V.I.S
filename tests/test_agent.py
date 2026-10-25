"""Planning agent: understanding, validation, execution, recovery, providers, persistence.

Every action goes through FakeControl, so no test opens, mutes, locks or shuts down anything.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis.core.agent import (Agent, Executor, Infeasible, LLMPlanner, PlanError, Step, build_registry,
                               split_compound, validate_plan)
from jarvis.core.agent.planner import extract_json
from jarvis.core.providers import CONNECTED, DISCONNECTED, ProviderStatus
from jarvis.core.store import Store
from jarvis.integrations.system_control import SystemControl

ACTIONS = ("volume_up", "volume_down", "volume_mute", "media_play_pause", "media_next", "media_prev",
           "media_stop", "get_battery", "get_uptime", "get_active_window", "cancel_power_action",
           "lock_workstation", "sleep_pc", "sign_out", "restart", "shutdown")


class FakeControl(SystemControl):
    """Real command grammar (resolve), fake actions that only record what was asked."""

    def __init__(self, fail=()):
        super().__init__()
        self.calls = []
        self.fail = set(fail)
        for name in ACTIONS:
            setattr(self, name, self._make(name))

    def _make(self, name):
        def act():
            self.calls.append(name)
            return f"Could not {name}." if name in self.fail else f"{name} ok."
        return act

    def open_target(self, name):
        self.calls.append(f"open:{name}")
        return f"Could not open {name}." if "open_target" in self.fail else f"Opened {name}."


class FakeLLM:
    supports_json = True

    def __init__(self, replies, name="ollama", state=CONNECTED):
        self.replies = list(replies)
        self.name = name
        self.state = state
        self.prompts = []

    def status(self):
        return ProviderStatus(self.state, "fake")

    def generate(self, prompt, *, system=None, json_mode=False):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def plan_json(*steps):
    return json.dumps({"steps": [dict(id=i + 1, depends_on=[], success="", **s) for i, s in enumerate(steps)]})


class CompoundTests(unittest.TestCase):
    def setUp(self):
        self.agent = Agent(FakeControl())

    def test_compound_command_splits_without_a_model(self):
        steps = self.agent.compound_steps("open youtube and then mute")
        self.assertEqual([(s.tool, s.args) for s in steps],
                         [("open_target", {"name": "youtube"}), ("volume_mute", {})])

    def test_launch_verb_carries_to_a_bare_name(self):
        steps = self.agent.compound_steps("open notepad and calculator")
        self.assertEqual([s.args["name"] for s in steps], ["notepad", "calculator"])

    def test_ordinary_sentences_are_never_split(self):
        for text in ("the difference between cats and dogs", "rock and roll", "why is the sky blue"):
            self.assertIsNone(self.agent.compound_steps(text), text)

    def test_unsupported_action_is_not_guessed(self):
        """'type hello' is its own verb, so it must not become 'open type hello'."""
        self.assertIsNone(self.agent.compound_steps("open notepad and type hello"))
        self.assertTrue(self.agent.wants("open notepad and type hello"))    # goes to the planner instead

    def test_classification(self):
        self.assertTrue(self.agent.wants("resume task"))
        self.assertTrue(self.agent.wants("task status"))
        self.assertTrue(self.agent.is_goal("get my workspace ready for coding"))
        self.assertFalse(self.agent.is_goal("why is the sky blue"))
        self.assertFalse(self.agent.wants("why is the sky blue"))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry(FakeControl())

    def test_unknown_tool_rejected(self):
        with self.assertRaisesRegex(PlanError, "not an available tool"):
            validate_plan({"steps": [{"tool": "format_disk", "args": {}}]}, self.reg)

    def test_wrong_arguments_rejected(self):
        with self.assertRaisesRegex(PlanError, "wrong arguments"):
            validate_plan({"steps": [{"tool": "open_target", "args": {"app": "x"}}]}, self.reg)

    def test_dependency_on_later_or_missing_step_rejected(self):
        with self.assertRaisesRegex(PlanError, "missing or comes later"):
            validate_plan({"steps": [{"id": 1, "tool": "volume_up", "args": {}, "depends_on": [2]},
                                     {"id": 2, "tool": "volume_down", "args": {}}]}, self.reg)

    def test_too_many_steps_rejected(self):
        with self.assertRaisesRegex(PlanError, "limit"):
            validate_plan({"steps": [{"tool": "volume_up", "args": {}}] * 9}, self.reg)

    def test_empty_plan_is_infeasible(self):
        with self.assertRaises(Infeasible):
            validate_plan({"steps": [], "reason": "no email tool"}, self.reg)

    def test_power_actions_move_last_and_ids_renumber(self):
        steps = validate_plan({"steps": [{"id": 1, "tool": "shutdown", "args": {}},
                                         {"id": 2, "tool": "volume_mute", "args": {}},
                                         {"id": 3, "tool": "get_battery", "args": {}, "depends_on": [2]}]},
                              self.reg)
        self.assertEqual([s.tool for s in steps], ["volume_mute", "get_battery", "shutdown"])
        self.assertEqual([s.id for s in steps], [1, 2, 3])
        self.assertEqual(steps[1].depends_on, [1])

    def test_depending_on_a_power_action_rejected(self):
        with self.assertRaisesRegex(PlanError, "must come last"):
            validate_plan({"steps": [{"id": 1, "tool": "sleep_pc", "args": {}},
                                     {"id": 2, "tool": "volume_up", "args": {}, "depends_on": [1]}]}, self.reg)

    def test_unrequested_power_action_is_rejected(self):
        """qwen3.5:2b planned 'restart' for 'get my workspace ready for coding' in testing."""
        with self.assertRaisesRegex(PlanError, "never asked"):
            validate_plan({"steps": [{"tool": "open_target", "args": {"name": "vscode"}},
                                     {"tool": "restart", "args": {}}]},
                          self.reg, goal="get my workspace ready for coding")

    def test_requested_power_action_runs_without_confirmation(self):
        steps = validate_plan({"steps": [{"tool": "volume_mute", "args": {}}, {"tool": "shutdown", "args": {}}]},
                              self.reg, goal="mute everything and then shut down the pc")
        self.assertEqual([s.tool for s in steps], ["volume_mute", "shutdown"])

    def test_tool_names_are_normalised(self):
        steps = validate_plan({"steps": [{"tool": " Volume_Mute() ", "args": {}}]}, self.reg)
        self.assertEqual(steps[0].tool, "volume_mute")

    def test_json_is_extracted_from_chatty_output(self):
        data = extract_json('Sure! ```json\n{"steps": []}\n``` hope that helps')
        self.assertEqual(data, {"steps": []})
        with self.assertRaises(PlanError):
            extract_json("I cannot help with that")


class ExecutorTests(unittest.TestCase):
    def run_steps(self, steps, fail=(), **kw):
        sc = FakeControl(fail=fail)
        report = Executor(build_registry(sc)).execute(None, steps, **kw)
        return sc, report

    def test_runs_in_order_and_reports_truthfully(self):
        sc, report = self.run_steps([Step(1, "open_target", {"name": "youtube"}), Step(2, "volume_mute")])
        self.assertEqual(sc.calls, ["open:youtube", "volume_mute"])
        self.assertEqual(report.status, "done")
        self.assertIn("2 of 2", report.summary)

    def test_failed_step_retried_once_then_reported_failed(self):
        sc, report = self.run_steps([Step(1, "volume_up")], fail={"volume_up"})
        self.assertEqual(sc.calls, ["volume_up", "volume_up"])      # one retry, bounded
        self.assertEqual(report.steps[0].status, "failed")
        self.assertEqual(report.status, "failed")
        self.assertIn("Failed", report.summary)
        self.assertNotIn("1 of 1", report.summary)

    def test_power_actions_are_never_retried(self):
        sc, report = self.run_steps([Step(1, "shutdown")], fail={"shutdown"})
        self.assertEqual(sc.calls, ["shutdown"])
        self.assertEqual(report.steps[0].attempts, 1)

    def test_dependents_of_a_failure_are_skipped_not_run(self):
        sc, report = self.run_steps([Step(1, "volume_up"), Step(2, "volume_down", depends_on=[1]),
                                     Step(3, "volume_mute")], fail={"volume_up"})
        self.assertNotIn("volume_down", sc.calls)
        self.assertEqual([s.status for s in report.steps], ["failed", "skipped", "done"])
        self.assertEqual(report.status, "partial")

    def test_destructive_steps_run_without_confirmation(self):
        """The owner chose full access: a planned shutdown executes, no prompt."""
        sc, report = self.run_steps([Step(1, "volume_mute"), Step(2, "shutdown")])
        self.assertEqual(sc.calls, ["volume_mute", "shutdown"])
        self.assertEqual(report.status, "done")

    def test_one_replan_routes_around_a_failure(self):
        seen = {}

        def replan(failed, done, remaining):
            seen["failed"] = failed.tool
            return [Step(10, "volume_down")]

        sc, report = self.run_steps([Step(1, "volume_up"), Step(2, "media_next")],
                                    fail={"volume_up"}, replan=replan)
        self.assertEqual(seen["failed"], "volume_up")
        self.assertEqual(report.steps[1].status, "replaced")
        self.assertNotIn("media_next", sc.calls)
        self.assertEqual(sc.calls[-1], "volume_down")

    def test_replanning_is_bounded(self):
        calls = []

        def replan(failed, done, remaining):
            calls.append(failed.id)
            return [Step(20 + len(calls), "volume_up")]

        self.run_steps([Step(1, "volume_up")], fail={"volume_up"}, replan=replan)
        self.assertEqual(len(calls), 1)


class ProviderStrategyTests(unittest.TestCase):
    def agent(self, *, ollama=None, chat=None, chat_ok=False):
        return Agent(FakeControl(), chat_provider=lambda: chat, chat_connected=lambda: chat_ok,
                     ollama_factory=(lambda: ollama) if ollama is not None else None)

    def test_ollama_is_the_planner(self):
        names = [p.name for p in self.agent(ollama=FakeLLM([])).planners()]
        self.assertEqual(names, ["ollama"])

    def test_claude_only_when_it_is_the_users_active_provider(self):
        claude = FakeLLM([], name="claude-code")
        self.assertEqual([p.name for p in self.agent(ollama=FakeLLM([]), chat=claude, chat_ok=True).planners()],
                         ["ollama", "claude-code"])
        self.assertEqual([p.name for p in self.agent(ollama=FakeLLM([]), chat=claude, chat_ok=False).planners()],
                         ["ollama"])
        ollama_chat = FakeLLM([], name="ollama")
        self.assertNotIn("claude-code", [p.name for p in self.agent(chat=ollama_chat, chat_ok=True).planners()])

    def test_disconnected_ollama_is_skipped(self):
        self.assertEqual(self.agent(ollama=FakeLLM([], state=DISCONNECTED)).planners(), [])

    def test_no_planner_reports_honestly(self):
        out = self.agent().handle("get my workspace ready for coding")
        self.assertIn("couldn't plan", out)
        self.assertIn("Ollama", out)

    def test_claude_escalation_only_after_ollama_fails(self):
        ollama = FakeLLM(["not json", "still not json"])
        claude = FakeLLM([plan_json({"tool": "volume_up", "args": {}})], name="claude-code")
        a = self.agent(ollama=ollama, chat=claude, chat_ok=True)
        out = a.handle("make it louder please")
        self.assertEqual(len(ollama.prompts), 2)      # one correction round on Ollama first
        self.assertEqual(len(claude.prompts), 1)
        self.assertIn("1 of 1", out)
        self.assertEqual(a.system_control.calls, ["volume_up"])


class AgentRunTests(unittest.TestCase):
    def test_goal_is_planned_executed_and_summarised(self):
        ollama = FakeLLM([plan_json({"tool": "open_target", "args": {"name": "vscode"}},
                                    {"tool": "volume_mute", "args": {}})])
        a = Agent(FakeControl(), ollama_factory=lambda: ollama)
        out = a.handle("get my workspace ready for coding")
        self.assertEqual(a.system_control.calls, ["open:vscode", "volume_mute"])
        self.assertIn("2 of 2", out)

    def test_rejected_plan_gets_one_correction_with_feedback(self):
        ollama = FakeLLM([plan_json({"tool": "teleport", "args": {}}),
                          plan_json({"tool": "volume_up", "args": {}})])
        a = Agent(FakeControl(), ollama_factory=lambda: ollama)
        a.handle("set up the room")
        self.assertIn("rejected", ollama.prompts[1])
        self.assertEqual(a.system_control.calls, ["volume_up"])

    def test_infeasible_goal_is_reported_not_retried(self):
        ollama = FakeLLM([json.dumps({"steps": [], "reason": "there is no email tool"})])
        out = Agent(FakeControl(), ollama_factory=lambda: ollama).handle("send an email to mom")
        self.assertIn("no email tool", out)
        self.assertEqual(len(ollama.prompts), 1)

    def test_compound_uses_no_model(self):
        ollama = FakeLLM([])
        a = Agent(FakeControl(), ollama_factory=lambda: ollama)
        out = a.handle("open youtube and then mute")
        self.assertEqual(ollama.prompts, [])
        self.assertEqual(a.system_control.calls, ["open:youtube", "volume_mute"])
        self.assertIn("2 of 2", out)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(None)
        self.addCleanup(self.store.close)

    def test_task_and_step_results_are_persisted(self):
        a = Agent(FakeControl(), store=self.store)
        a.handle("open youtube and then mute")
        t = self.store.agent_tasks(limit=1)[0]
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["planner"], "local")
        self.assertEqual([(s["tool"], s["status"], s["attempts"]) for s in t["steps"]],
                         [("open_target", "done", 1), ("volume_mute", "done", 1)])
        self.assertIn("Opened youtube", t["steps"][0]["output"])

    def test_interrupted_task_resumes_without_repeating_power_actions(self):
        tid = self.store.create_agent_task("tidy up and shut down", "ollama")
        self.store.save_agent_steps(tid, [
            Step(1, "volume_mute", status="done", attempts=1, output="volume_mute ok.").as_dict(),
            Step(2, "volume_down", status="running", attempts=1).as_dict(),     # crashed mid-step
            Step(3, "shutdown", status="running", attempts=1).as_dict(),        # may have fired
            Step(4, "media_stop").as_dict(),
        ])
        self.store.update_agent_task(tid, status="running")

        sc = FakeControl()
        a = Agent(sc, store=self.store)          # startup marks the task interrupted
        self.assertEqual(self.store.agent_task(tid)["status"], "interrupted")
        out = a.resume()

        self.assertIn(f"#{tid}", out)
        self.assertEqual(sc.calls, ["volume_down", "media_stop"])     # idempotent re-run, power not repeated
        steps = {s["id"]: s for s in self.store.agent_task(tid)["steps"]}
        self.assertEqual(steps[1]["status"], "done")
        self.assertEqual(steps[3]["status"], "skipped")
        self.assertIn("may already have run", steps[3]["output"])

    def test_nothing_to_resume(self):
        self.assertIn("no interrupted task", Agent(FakeControl(), store=self.store).resume())

    def test_status_report(self):
        a = Agent(FakeControl(), store=self.store)
        a.handle("open youtube and then mute")
        self.assertIn("done", a.handle("task status"))


if __name__ == "__main__":
    unittest.main()

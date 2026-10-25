"""Security layer, service framework, Google tools and the confirmation flow.

No test reaches the network, a browser, or a real account: Google is a MagicMock API, the vault
lives in a temp dir (it does use the real Windows DPAPI), and PC actions are recorded, not run.
"""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from jarvis.core.agent import Agent, Step
from jarvis.core.agent.executor import Executor
from jarvis.core.agent.tools import Tool, build_registry
from jarvis.core.security import ActionLog, Policy, Vault, VaultError
from jarvis.core.store import Store
from jarvis.integrations.services import GoogleService, load_services
from jarvis.integrations.services.google import TOKEN_KEY
from tests.test_agent import FakeControl


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "vault.bin"

    def test_roundtrip_and_delete(self):
        v = Vault(self.path)
        v.set("token", {"refresh": "abc123", "n": 1})
        self.assertEqual(Vault(self.path).get("token"), {"refresh": "abc123", "n": 1})   # survives a new instance
        self.assertEqual(v.keys(), ["token"])
        self.assertTrue(v.delete("token"))
        self.assertIsNone(v.get("token"))
        self.assertFalse(v.delete("token"))

    def test_nothing_is_stored_in_plain_text(self):
        Vault(self.path).set("token", {"refresh": "SECRET-REFRESH-TOKEN"})
        raw = self.path.read_bytes()
        self.assertNotIn(b"SECRET-REFRESH-TOKEN", raw)
        self.assertNotIn(b"refresh", raw)

    def test_tampered_vault_is_an_error_not_silent_data_loss(self):
        v = Vault(self.path)
        v.set("k", "v")
        self.path.write_bytes(b"not a dpapi blob at all")
        with self.assertRaises(VaultError):
            v.get("k")

    def test_missing_vault_is_simply_empty(self):
        self.assertIsNone(Vault(self.path).get("anything"))


class PolicyAndLogTests(unittest.TestCase):
    def tool(self, name, risk):
        return Tool(name, "x", lambda: "ok", risk=risk)

    def test_defaults_by_risk(self):
        p = Policy()
        self.assertEqual(p.decide(self.tool("volume_up", "low")).level, "allow")
        self.assertEqual(p.decide(self.tool("gmail_send", "consequential")).level, "confirm")
        self.assertEqual(p.decide(self.tool("weird", "mystery")).level, "confirm")     # unknown risk: ask

    def test_overrides_win_and_bad_values_are_ignored(self):
        p = Policy({"gmail_send": "deny", "volume_up": "confirm", "x": "banana"})
        self.assertEqual(p.decide(self.tool("gmail_send", "consequential")).level, "deny")
        self.assertEqual(p.decide(self.tool("volume_up", "low")).level, "confirm")
        self.assertNotIn("x", p.overrides)

    def test_policy_file_loading(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "permissions.json"
            f.write_text(json.dumps({"tools": {"gmail_send": "allow"}}), encoding="utf-8")
            self.assertEqual(Policy.load(f).overrides, {"gmail_send": "allow"})
            self.assertEqual(Policy.load(Path(d) / "missing.json").overrides, {})
            f.write_text("{broken", encoding="utf-8")
            self.assertEqual(Policy.load(f).overrides, {})

    def test_action_log_records_redacts_and_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            log = ActionLog(Path(d) / "a.jsonl")
            log.record(tool="gmail_send", args={"to": "a@b.co", "body": "api_key=abcdef1234567890"},
                       decision="approved", outcome="ok", task_id=7)
            entry = log.tail()[0]
            self.assertEqual((entry["tool"], entry["decision"], entry["outcome"], entry["task"]),
                             ("gmail_send", "approved", "ok", 7))
            self.assertNotIn("abcdef1234567890", json.dumps(entry))
        ActionLog(Path("Z:/no/such/drive/a.jsonl")).record(tool="t", args={}, decision="allow", outcome="ok")


class ConfirmationFlowTests(unittest.TestCase):
    """A consequential step pauses; only an explicit yes lets it run."""

    def setUp(self):
        self.sc = FakeControl()
        self.sent = []

        def send(to, subject, body):
            self.sent.append((to, subject))
            return f"Sent email to {to}."

        self.send_tool = Tool("gmail_send", "Send an email.", send,
                              {"to": "x", "subject": "x", "body": "x"}, risk="consequential", retryable=False,
                              describe=lambda a: f"send an email to {a['to']} with the subject '{a['subject']}'")
        self.store = Store(None)
        self.addCleanup(self.store.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = ActionLog(Path(self.tmp.name) / "a.jsonl")

    def executor(self, policy=None):
        reg = build_registry(self.sc, extra_tools=(self.send_tool,))
        return reg, Executor(reg, store=self.store, policy=policy or Policy(), actionlog=self.log)

    def steps(self):
        return [Step(1, "volume_mute"),
                Step(2, "gmail_send", {"to": "a@b.co", "subject": "Hi", "body": "hello"}),
                Step(3, "volume_up")]

    def test_consequential_step_pauses_and_nothing_after_it_runs(self):
        _, ex = self.executor()
        report = ex.execute(None, self.steps())
        self.assertEqual(report.status, "awaiting")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.sc.calls, ["volume_mute"])
        self.assertEqual([s.status for s in report.steps], ["done", "awaiting", "pending"])
        self.assertIn("send an email to a@b.co", report.summary)

    def test_approval_resumes_and_finishes_the_rest(self):
        _, ex = self.executor()
        steps = self.steps()
        ex.execute(None, steps)
        steps[1].status = "pending"
        report = ex.execute(None, steps, approved={2})
        self.assertEqual(report.status, "done")
        self.assertEqual(self.sent, [("a@b.co", "Hi")])
        self.assertEqual(self.sc.calls, ["volume_mute", "volume_up"])

    def test_deny_policy_blocks_without_asking(self):
        _, ex = self.executor(Policy({"gmail_send": "deny"}))
        report = ex.execute(None, self.steps())
        self.assertEqual(self.sent, [])
        self.assertEqual(report.steps[1].status, "failed")
        self.assertIn("Blocked", report.steps[1].output)
        self.assertEqual(report.steps[2].status, "done")      # an unrelated later step still runs

    def test_allow_override_skips_confirmation(self):
        _, ex = self.executor(Policy({"gmail_send": "allow"}))
        self.assertEqual(ex.execute(None, self.steps()).status, "done")
        self.assertEqual(len(self.sent), 1)

    def test_pc_power_actions_stay_unconfirmed_by_the_owners_choice(self):
        _, ex = self.executor()
        self.assertEqual(ex.execute(None, [Step(1, "sleep_pc")]).status, "done")

    def test_every_decision_is_logged(self):
        _, ex = self.executor()
        steps = self.steps()
        ex.execute(None, steps)
        steps[1].status = "pending"
        ex.execute(None, steps, approved={2})
        got = [(e["tool"], e["decision"], e["outcome"]) for e in self.log.tail()]
        self.assertIn(("gmail_send", "confirm", "requested"), got)
        self.assertIn(("gmail_send", "approved", "ok"), got)
        self.assertIn(("volume_mute", "allow", "ok"), got)

    def agent(self):
        a = Agent(self.sc, store=self.store, services=[_Fake(self.send_tool)], policy=Policy(), actionlog=self.log)
        return a

    def test_agent_asks_then_yes_sends_exactly_once(self):
        a = self.agent()
        out = a.handle("mute and then email bob")
        self.assertEqual(self.sent, [])
        self.assertIn("Say 'yes'", out)
        self.assertTrue(a.has_pending())
        self.assertTrue(a.wants("yes"))
        out = a.handle("yes")
        self.assertEqual(self.sent, [("bob@example.com", "Hello")])
        self.assertFalse(a.has_pending())
        self.assertFalse(a.wants("yes"))                 # a stray "yes" later is not routed here
        a.handle("yes")
        self.assertEqual(len(self.sent), 1)

    def test_agent_no_cancels_and_sends_nothing(self):
        a = self.agent()
        a.handle("mute and then email bob")
        out = a.handle("no")
        self.assertIn("Cancelled", out)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.agent_tasks(limit=1)[0]["status"], "cancelled")

    def test_confirmation_survives_a_restart(self):
        a = self.agent()
        a.handle("mute and then email bob")
        self.assertEqual(self.store.agent_tasks(limit=1)[0]["status"], "awaiting_confirmation")
        b = self.agent()                                   # new process, same database
        self.assertTrue(b.has_pending())
        b.handle("yes")
        self.assertEqual(self.sent, [("bob@example.com", "Hello")])

    def test_yes_and_no_mean_nothing_without_a_pending_action(self):
        a = self.agent()
        self.assertFalse(a.wants("yes"))
        self.assertFalse(a.wants("no"))
        self.assertIn("nothing", a.confirm().lower())


class _Fake:
    """A service whose only phrase is 'email bob' -> a consequential send."""
    name = "fake"

    def __init__(self, tool):
        self._tool = tool

    def tools(self):
        return [self._tool]

    def resolve(self, text):
        if text.strip().lower().rstrip(".") == "email bob":
            return "gmail_send", {"to": "bob@example.com", "subject": "Hello", "body": "Hi Bob"}
        return None


class GoogleServiceTests(unittest.TestCase):
    def google(self, api=None, token=True):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        vault = Vault(Path(d.name) / "v.bin")
        if token:
            vault.set(TOKEN_KEY, {"token": "t"})
        api = api if api is not None else mock.MagicMock()
        return GoogleService(vault, build=lambda name, version: api,
                             now=lambda: datetime(2026, 10, 27, 9, 0)), api

    def test_not_connected_says_how_to_connect(self):
        g = GoogleService(Vault(Path(tempfile.mkdtemp()) / "v.bin"))
        self.assertFalse(g.configured())
        self.assertIn("connect google", g.gmail_search("is:unread").lower())
        self.assertIn("connect google", g.calendar_agenda("1").lower())

    def test_not_connected_is_reported_as_a_failure_not_a_success(self):
        """It used to read 'Google is not connected' under '1 of 1 step(s) succeeded'."""
        g = GoogleService(Vault(Path(tempfile.mkdtemp()) / "v.bin"))
        out = Agent(FakeControl(), services=[g], policy=Policy()).handle("check my email")
        self.assertIn("0 of 1", out)
        self.assertIn("Failed", out)
        self.assertNotIn("1 of 1", out)

    def test_gmail_search_formats_sender_and_subject(self):
        g, api = self.google()
        api.users().messages().list().execute.return_value = {"messages": [{"id": "1"}]}
        api.users().messages().get().execute.return_value = {"payload": {"headers": [
            {"name": "From", "value": '"Alice" <alice@x.com>'}, {"name": "Subject", "value": "Lunch?"}]}}
        out = g.gmail_search("is:unread")
        self.assertIn("Alice: Lunch?", out)
        self.assertNotIn("alice@x.com", out)

    def test_gmail_search_empty(self):
        g, api = self.google()
        api.users().messages().list().execute.return_value = {}
        self.assertIn("No emails found", g.gmail_search("from:nobody"))

    def test_send_validates_the_address_before_calling_google(self):
        g, api = self.google()
        self.assertIn("Invalid email", g.gmail_send("not-an-address", "s", "b"))
        self.assertIn("Invalid email", g.gmail_send("a@b.co, c@d.co", "s", "b"))
        api.users().messages().send.assert_not_called()

    def test_send_builds_a_real_message(self):
        import base64
        g, api = self.google()
        api.users().messages().send().execute.return_value = {"id": "m9"}
        out = g.gmail_send("bob@example.com", "Hello", "Hi Bob")
        self.assertIn("Sent email to bob@example.com", out)
        raw = api.users().messages().send.call_args.kwargs["body"]["raw"]
        text = base64.urlsafe_b64decode(raw + "==").decode()
        self.assertIn("To: bob@example.com", text)
        self.assertIn("Subject: Hello", text)
        self.assertIn("Hi Bob", text)

    def test_calendar_agenda_and_empty(self):
        g, api = self.google()
        api.events().list().execute.return_value = {"items": [
            {"summary": "Standup", "start": {"dateTime": "2026-10-27T10:30:00+05:30"}}]}
        self.assertIn("Standup", g.calendar_agenda("1"))
        api.events().list().execute.return_value = {"items": []}
        self.assertIn("Nothing on your calendar today", g.calendar_agenda("1"))
        self.assertIn("Invalid", g.calendar_agenda("lots"))

    def test_calendar_create_validates_time(self):
        g, api = self.google()
        self.assertIn("Invalid start time", g.calendar_create("Dentist", "next tuesday-ish", "30"))
        api.events().insert.assert_not_called()
        api.events().insert().execute.return_value = {"id": "e1"}
        self.assertIn("Added 'Dentist'", g.calendar_create("Dentist", "2026-10-28 15:00", "30"))
        body = api.events().insert.call_args.kwargs["body"]
        self.assertEqual(body["summary"], "Dentist")
        self.assertIn("2026-10-28T15:00", body["start"]["dateTime"])

    def test_drive_query_is_escaped(self):
        g, api = self.google()
        api.files().list().execute.return_value = {"files": [{"name": "Report.pdf"}]}
        self.assertIn("Report.pdf", g.drive_search("it's a trap"))
        self.assertIn("it\\'s a trap", api.files().list.call_args.kwargs["q"])

    def test_api_errors_are_reported_not_raised(self):
        g, api = self.google()
        api.users().messages().list().execute.side_effect = RuntimeError("quota")
        self.assertIn("Could not complete", g.gmail_search("x"))

    def test_consequential_tools_are_marked(self):
        g, _ = self.google()
        risk = {t.name: t.risk for t in g.tools()}
        self.assertEqual(risk["gmail_send"], "consequential")
        self.assertEqual(risk["calendar_create"], "consequential")
        self.assertEqual(risk["gmail_search"], "low")
        self.assertEqual(risk["drive_search"], "low")

    def test_connect_needs_the_client_file(self):
        g = GoogleService(Vault(Path(tempfile.mkdtemp()) / "v.bin"), secret_path=Path("Z:/none.json"))
        self.assertIn("OAuth client file", g.connect())

    def test_connect_stores_the_token_in_the_vault(self):
        d = Path(tempfile.mkdtemp())
        secret = d / "client.json"
        secret.write_text("{}", encoding="utf-8")
        vault = Vault(d / "v.bin")
        g = GoogleService(vault, secret_path=secret, flow_runner=lambda p, scopes: {"token": "fresh", "n": len(scopes)})
        self.assertIn("connected", g.connect())
        self.assertEqual(vault.get(TOKEN_KEY)["token"], "fresh")
        self.assertTrue(g.configured())

    def test_phrases_resolve_without_a_model(self):
        g, _ = self.google()
        cases = {
            "check my email": ("gmail_search", {"query": "is:unread in:inbox"}),
            "Any new emails?": ("gmail_search", {"query": "is:unread in:inbox"}),
            "search my email for invoices": ("gmail_search", {"query": "invoices"}),
            "what's on my calendar tomorrow": ("calendar_agenda", {"days": "2"}),
            "what's on my calendar": ("calendar_agenda", {"days": "1"}),
            "my schedule this week": ("calendar_agenda", {"days": "7"}),
            "search drive for tax return": ("drive_search", {"query": "tax return"}),
            "find budget in my drive": ("drive_search", {"query": "budget"}),
            "connect google": ("google_connect", {}),
        }
        for text, want in cases.items():
            self.assertEqual(g.resolve(text), want, text)
        for text in ("why is the sky blue", "open youtube", "check the battery", "email is great"):
            self.assertIsNone(g.resolve(text), text)

    def test_agent_runs_google_phrases_end_to_end(self):
        g, api = self.google()
        api.users().messages().list().execute.return_value = {"messages": []}
        a = Agent(FakeControl(), services=[g], policy=Policy())
        self.assertTrue(a.wants("check my email"))
        self.assertIn("No emails found", a.handle("check my email"))
        self.assertIn("1 of 1", a.handle("check my email"))

    def test_registry_loads_google_by_default(self):
        self.assertEqual([s.name for s in load_services(None)], ["google"])


if __name__ == "__main__":
    unittest.main()

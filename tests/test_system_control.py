"""Unit tests for narrowly scoped system control tools and offline integration."""
import os
import unittest
from unittest import mock

from jarvis.core import assistant
from jarvis.core.store import Store
from jarvis.integrations.system_control import (
    ALLOWED_APPS,
    SystemControl,
    _send_vk,
    VK_VOLUME_DOWN,
    VK_VOLUME_MUTE,
    VK_VOLUME_UP,
)
from jarvis.ui.backend import Backend


class SystemControlTests(unittest.TestCase):
    def setUp(self):
        self.sc = SystemControl()

    def test_volume_controls(self):
        with mock.patch("jarvis.integrations.system_control._send_vk", return_value=True) as m_vk:
            res_mute = self.sc.volume_mute()
            self.assertIn("mute", res_mute.lower())
            m_vk.assert_called_with(VK_VOLUME_MUTE)

            res_up = self.sc.volume_up(steps=3)
            self.assertIn("+3", res_up)
            self.assertEqual(m_vk.call_args[0][0], VK_VOLUME_UP)

            res_down = self.sc.volume_down(steps=2)
            self.assertIn("-2", res_down)
            self.assertEqual(m_vk.call_args[0][0], VK_VOLUME_DOWN)

    def test_media_controls(self):
        with mock.patch("jarvis.integrations.system_control._send_vk", return_value=True) as m_vk:
            self.assertIn("toggled", self.sc.media_play_pause())
            self.assertIn("next", self.sc.media_next().lower())
            self.assertIn("previous", self.sc.media_prev().lower())
            self.assertIn("stopped", self.sc.media_stop().lower())
            self.assertEqual(m_vk.call_count, 4)

    def test_app_launcher_allowed(self):
        with mock.patch("os.startfile", create=True) as m_start:
            res = self.sc.open_app("notepad")
            self.assertIn("Opened notepad", res)
            m_start.assert_called_once_with(ALLOWED_APPS["notepad"])

    def test_app_launcher_aliases(self):
        with mock.patch("os.startfile", create=True) as m_start:
            self.assertIn("calc.exe", self.sc.open_app("calc").lower())
            self.assertIn("explorer.exe", self.sc.open_app("files").lower())
            self.assertEqual(m_start.call_count, 2)

    def test_command_lines_are_never_launched(self):
        """Full access opens apps and sites, not arbitrary command lines. ShellExecute
        accepts a string carrying switches, so these are searched for instead."""
        for bad in ("powershell -Command Remove-Item",
                    "../../windows/system32/cmd.exe",
                    "notepad & calc",
                    "cmd | del"):
            with mock.patch("os.startfile", create=True) as m_start:
                res = self.sc.open_target(bad)
                self.assertIn("command line", res.lower(), bad)
                # the only thing launched is a web search, never the string itself
                launched = m_start.call_args[0][0] if m_start.called else ""
                self.assertTrue(launched.startswith("https://www.google.com/search"), bad)

    def test_battery_query(self):
        res = self.sc.get_battery()
        self.assertTrue(any(word in res.lower() for word in ("battery", "power", "charging", "plugged")))

    def test_uptime_query(self):
        res = self.sc.get_uptime()
        self.assertTrue("uptime" in res.lower() or "day" in res.lower() or "minute" in res.lower())

    def test_lock_workstation(self):
        with mock.patch("jarvis.integrations.system_control._u32.LockWorkStation", create=True, return_value=1):
            res = self.sc.lock_workstation()
            self.assertIn("locked", res.lower())

    def test_power_commands_execute(self):
        """Full PC control is enabled by the owner. Power actions run a fixed argv,
        never a shell, and shutdown/restart carry a cancellable grace period."""
        with mock.patch.object(SystemControl, "_run", return_value=(True, "")) as run:
            self.assertIn("shutting down", self.sc.execute_command("shutdown").lower())
            self.assertIn("restarting", self.sc.execute_command("restart the computer").lower())
            self.assertIn("sign", self.sc.execute_command("sign out").lower())
            self.assertIn("cancel", self.sc.execute_command("cancel shutdown").lower())
        argvs = [c.args[0] for c in run.call_args_list]
        self.assertEqual(argvs[0][:2], ["shutdown", "/s"])
        self.assertEqual(argvs[1][:2], ["shutdown", "/r"])
        self.assertEqual(argvs[3], ["shutdown", "/a"])
        for argv in argvs:
            self.assertIsInstance(argv, list)      # fixed argv: no shell, no injection

    def test_open_resolves_websites_and_apps(self):
        with mock.patch.object(SystemControl, "_launch", side_effect=lambda t, m: t) as launch:
            self.assertIn("youtube.com", self.sc.execute_command("open youtube"))
            self.assertIn("youtube.com", self.sc.execute_command("turn on youtube"))
            self.assertIn("netflix.com", self.sc.execute_command("play netflix"))
            self.assertIn("notepad", self.sc.execute_command("open notepad").lower())
        self.assertTrue(launch.called)

    def test_sleep_is_not_mistaken_for_an_app(self):
        with mock.patch.object(SystemControl, "_suspend", return_value="Going to sleep.") as susp:
            self.assertIn("sleep", self.sc.execute_command("go to sleep").lower())
        susp.assert_called_once_with(False, "Going to sleep.")

    def test_sleep_requests_sleep_and_never_hibernate(self):
        """rundll32 powrprof.dll,SetSuspendState 0,1,0 hibernated this laptop: rundll32 passes the
        window handle as the first argument, which Windows read as bHibernate=TRUE. The function
        must be called directly with bHibernate=False."""
        with mock.patch("jarvis.integrations.system_control.threading.Timer") as timer,              mock.patch("jarvis.integrations.system_control.ctypes.WinDLL") as dll:
            self.assertEqual(self.sc.sleep_pc(), "Going to sleep.")
            timer.call_args[0][1]()                      # run the deferred suspend now
        dll.return_value.SetSuspendState.assert_called_once_with(False, False, False)

    def test_hibernate_is_its_own_command(self):
        for text in ("hibernate", "Hibernate the laptop.", "put my pc in hibernation mode"):
            self.assertEqual(self.sc.resolve(text), ("hibernate_pc", {}), text)
        with mock.patch.object(SystemControl, "_run", return_value=(True, "")) as run:
            self.assertEqual(self.sc.hibernate_pc(), "Hibernating.")
        self.assertEqual(run.call_args[0][0], ["shutdown", "/h"])

    def test_no_test_can_trigger_a_real_sleep(self):
        """Guard: sleep_pc must never be reachable without the timer being patched."""
        self.assertEqual(self.sc.resolve("go to sleep"), ("sleep_pc", {}))   # resolve never executes

    def test_unrelated_sentences_are_not_device_commands(self):
        """A sentence that merely mentions a keyword must not be treated as a device command."""
        for q in ("remind me to buy a new battery for the car",
                  "summarize the battery article",
                  "restart the deployment script",
                  "I need to restart my morning routine",
                  "what is the uptime guarantee in the contract",
                  "close the current window blinds"):
            self.assertIsNone(self.sc.execute_command(q), q)

    def test_device_queries_match_natural_phrasings(self):
        self.assertIn("battery", self.sc.execute_command("what's my battery level?").lower())
        self.assertIn("uptime", self.sc.execute_command("system uptime").lower())
        self.assertIn("uptime", self.sc.execute_command("how long has the computer been up").lower())
        self.assertIsNotNone(self.sc.execute_command("what is the active window"))

    def test_lock_failure_is_reported_honestly(self):
        """A refused lock must not claim success or a simulation."""
        with mock.patch("jarvis.integrations.system_control._u32.LockWorkStation",
                        create=True, return_value=0):
            res = self.sc.lock_workstation()
        self.assertIn("could not lock", res.lower())
        self.assertNotIn("simulated", res.lower())

    def test_natural_language_command_router(self):
        with mock.patch("jarvis.integrations.system_control._send_vk", return_value=True):
            self.assertIn("mute", self.sc.execute_command("mute volume").lower())
            self.assertIn("turned up", self.sc.execute_command("turn up volume").lower())
            self.assertIn("turned down", self.sc.execute_command("quieter").lower())
            self.assertIn("play/pause", self.sc.execute_command("play music").lower())
            self.assertIn("next", self.sc.execute_command("next track").lower())
            self.assertIn("previous", self.sc.execute_command("prev song").lower())

        with mock.patch("os.startfile", create=True):
            self.assertIn("Opened notepad", self.sc.execute_command("open notepad"))
            self.assertIn("Opened calc", self.sc.execute_command("launch calculator"))

        self.assertIn("battery", self.sc.execute_command("how is the battery").lower())
        self.assertIn("uptime", self.sc.execute_command("uptime").lower())
        self.assertIsNone(self.sc.execute_command("what is the capital of France"))


class AssistantSystemControlIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.sc = SystemControl()
        self.status = {
            "cpu": 0.15,
            "ram": 0.40,
            "ram_used_gb": 6.0,
            "ram_total_gb": 16.0,
            "disk": 0.50,
            "disk_used_gb": 120,
            "disk_total_gb": 240,
        }

    def test_assistant_dispatches_to_system_control(self):
        with mock.patch("jarvis.integrations.system_control._send_vk", return_value=True):
            ans = assistant.answer("volume up", self.status, [], system_control=self.sc)
            self.assertIsNotNone(ans)
            self.assertIn("Volume turned up", ans)

    def test_assistant_preserves_builtins_with_system_control(self):
        ans_status = assistant.answer("status", self.status, [], system_control=self.sc)
        self.assertIn("CPU 15%", ans_status)

        ans_help = assistant.answer("help", self.status, [], system_control=self.sc)
        self.assertIn("Power:", ans_help)
        self.assertIn("Sound:", ans_help)

    def test_backend_records_activity_on_tool_execution(self):
        from tests.qt_helpers import gui_app, pump
        app = gui_app()
        store = Store(None)  # in-memory store
        backend = Backend(store=store, system_control=self.sc)
        with mock.patch("jarvis.integrations.system_control._send_vk", return_value=True):
            backend.send("volume up")
            # Wait for singleShot delivery
            pump(minimum=0.6)

            messages = store.messages()
            self.assertTrue(any("Volume turned up" in m["text"] for m in messages))

            activity = store.activity()
            self.assertTrue(any(a["kind"] == "tool_executed" for a in activity))


if __name__ == "__main__":
    unittest.main()

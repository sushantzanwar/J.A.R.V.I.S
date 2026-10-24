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

    def test_app_launcher_rejected(self):
        with mock.patch("os.startfile", create=True) as m_start:
            res = self.sc.open_app("powershell -Command Remove-Item")
            self.assertIn("not in the allowed list", res)
            m_start.assert_not_called()

    def test_app_launcher_path_traversal_rejected(self):
        with mock.patch("os.startfile", create=True) as m_start:
            res = self.sc.open_app("../../windows/system32/cmd.exe")
            self.assertIn("not in the allowed list", res)
            m_start.assert_not_called()

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

    def test_protected_destructive_actions(self):
        res_shutdown = self.sc.execute_command("shutdown now")
        self.assertIn("protected", res_shutdown.lower())
        self.assertIn("requires explicit human approval", res_shutdown.lower())

        res_restart = self.sc.execute_command("restart computer")
        self.assertIn("protected", res_restart.lower())

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
        self.assertIn("System controls", ans_help)

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

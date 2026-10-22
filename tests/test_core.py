import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from jarvis.core import assistant
from jarvis.core.system_status import SystemMonitor
from jarvis.core.tasks import awaiting_approval, read_tasks

CORE = Path(__file__).resolve().parents[1] / "jarvis" / "core"
STATUS = {"cpu": 0.25, "ram": 0.5, "ram_used_gb": 7.5, "ram_total_gb": 15.0,
          "disk": 0.4, "disk_used_gb": 100, "disk_total_gb": 250}


def make_task(d: Path, i: int, **kw):
    t = {"id": i, "title": f"t{i}", "status": "pending", "updated": "x", **kw}
    (d / f"{i:03d}.json").write_text(json.dumps(t), encoding="utf-8")


class SystemStatusTests(unittest.TestCase):
    def test_ranges(self):
        m = SystemMonitor()
        m.snapshot()
        s = m.snapshot()
        for k in ("cpu", "ram", "disk"):
            self.assertTrue(0.0 <= s[k] <= 1.0, k)
        self.assertGreater(s["ram_total_gb"], 0)
        self.assertGreater(s["disk_total_gb"], 0)


class TaskTests(unittest.TestCase):
    def test_read_only_and_robust(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            make_task(d, 1, status="done", approved=True)
            make_task(d, 2)
            (d / "003.json").write_text("{not json", encoding="utf-8")
            before = {p.name: p.read_bytes() for p in d.iterdir()}
            tasks = read_tasks(d)
            self.assertEqual([t["id"] for t in tasks], [2, 1])
            self.assertEqual([t["id"] for t in awaiting_approval(tasks)], [2])
            self.assertEqual(before, {p.name: p.read_bytes() for p in d.iterdir()})

    def test_missing_dir(self):
        self.assertEqual(read_tasks(Path("does/not/exist")), [])


class AssistantTests(unittest.TestCase):
    def r(self, text, tasks=()):
        return assistant.reply(text, STATUS, list(tasks), datetime(2026, 1, 2, 3, 4))

    def test_commands(self):
        self.assertIn("CPU 25%", self.r("status"))
        self.assertIn("03:04", self.r("what time is it"))
        self.assertIn("help", self.r("zzz").lower())
        self.assertIn("Say something", self.r("  "))

    def test_task_summary(self):
        tasks = [{"id": 7, "title": "x", "status": "pending", "approved": False, "needs": "read-only", "updated": ""}]
        self.assertIn("1 awaiting your approval", self.r("tasks", tasks))

    def test_cannot_approve(self):
        self.assertIn("can't approve", self.r("please approve task 7"))

    def test_core_has_no_exec_or_qt(self):
        banned = re.compile(r"\b(subprocess|os\.system|os\.popen|eval|exec)\b|\b(import|from)\s+PySide6")
        for p in CORE.glob("*.py"):
            self.assertIsNone(banned.search(p.read_text(encoding="utf-8")), p.name)

    def test_core_never_writes_files_except_the_local_store(self):
        banned = re.compile(r"write_text|write_bytes|open\([^)]*['\"][wa]|\.unlink|rmtree|os\.remove")
        for p in CORE.glob("*.py"):
            if p.name == "store.py":   # the one module allowed to write: its own SQLite file in the data folder
                continue
            self.assertIsNone(banned.search(p.read_text(encoding="utf-8")), p.name)

    def test_store_only_touches_its_own_database_files(self):
        src = (CORE / "store.py").read_text(encoding="utf-8")
        self.assertNotIn("rmtree", src)
        self.assertNotIn("os.remove", src)
        self.assertNotIn(".unlink", src)         # damaged files are renamed aside, never deleted


if __name__ == "__main__":
    unittest.main()

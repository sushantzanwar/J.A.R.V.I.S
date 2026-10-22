"""The setup / test / build scripts must stay inside the project and never touch the scheduler or approvals."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [ROOT / "scripts" / n for n in ("setup.ps1", "run-tests.ps1", "build.ps1")]
FORBIDDEN = re.compile(
    r"Register-ScheduledTask|Unregister-ScheduledTask|schtasks|Set-ExecutionPolicy|Start-Service|New-Service|"
    r"\bpip install --user|--break-system-packages|\bsetx\b|HKLM:|HKCU:|\[Environment\]::SetEnvironmentVariable|"
    r"admin\.py\s+approve|--allow-commands|Invoke-WebRequest|Invoke-RestMethod|\bcurl\b|\bnetsh\b|\bgit\s+push\b",
    re.IGNORECASE)


class ReleaseScriptTests(unittest.TestCase):
    def test_scripts_exist_and_are_documented(self):
        doc = (ROOT / "docs" / "RELEASE.md").read_text(encoding="utf-8")
        for p in SCRIPTS:
            self.assertTrue(p.is_file(), p.name)
            self.assertIn(p.name, doc, f"{p.name} is not documented in docs/RELEASE.md")

    def test_scripts_make_no_system_wide_changes(self):
        for p in SCRIPTS:
            self.assertIsNone(FORBIDDEN.search(p.read_text(encoding="utf-8")), p.name)

    def test_installs_only_go_through_the_project_venv(self):
        for p in SCRIPTS:
            text = p.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?m)^\s*(&\s*)?pip3?\s+install", f"{p.name}: bare pip install (global)")
            for line in text.splitlines():
                if re.search(r"-m pip install", line):      # a line that really runs pip
                    self.assertIn("$venvPython", line, f"{p.name}: install must use the project .venv: {line.strip()}")

    def test_dev_tools_are_separate_from_runtime_dependencies(self):
        runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        self.assertNotIn("pyinstaller", runtime.lower())
        self.assertIn("-r requirements.txt", dev)
        self.assertIn("pyinstaller", dev.lower())

    def test_build_outputs_and_local_state_are_git_ignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for rule in ("dist/", "build/", ".venv/", "*.db", "logs/", ".jarvis/", "bridge/tasks/"):
            self.assertIn(rule, ignore, rule)

    def test_spec_bundles_the_qml_and_only_the_ui(self):
        spec = (ROOT / "packaging" / "jarvis.spec").read_text(encoding="utf-8")
        self.assertIn("jarvis/ui/qml", spec)
        self.assertNotIn("bridge", spec.replace("bridge/ and the worker stay in the project folder", ""))
        self.assertIn("console=False", spec)

    def test_build_smoke_check_uses_a_throwaway_data_folder(self):
        text = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("JARVIS_DATA_DIR", text)


if __name__ == "__main__":
    unittest.main()

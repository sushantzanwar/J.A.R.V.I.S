"""Git worktree lifecycle for isolated file-change tasks.

No cleanup/reset is automatic: every task worktree and branch is retained for
diagnosis.  Integration is serialized and happens only when the target checkout
is clean, so unrelated user changes are never overwritten.
"""
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bridge"))
import tasklock  # noqa: E402


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class Workspace:
    task_id: int
    path: Path
    branch: str


class WorkspaceManager:
    def __init__(self, root: Path = ROOT, runner=subprocess.run):
        self.root = Path(root).resolve()
        self.base = self.root / ".jarvis" / "workspaces"
        self._run = runner

    def _git(self, args, cwd=None, check=True):
        try:
            return self._run(["git", *args], cwd=str(cwd or self.root), capture_output=True,
                             text=True, encoding="utf-8", errors="replace", check=check)
        except (OSError, subprocess.CalledProcessError) as e:
            detail = getattr(e, "stderr", "") or getattr(e, "stdout", "") or type(e).__name__
            raise WorkspaceError(str(detail).strip()[:500]) from None

    def available(self) -> bool:
        try:
            return self._git(["rev-parse", "--is-inside-work-tree"]).stdout.strip() == "true"
        except WorkspaceError:
            return False

    @staticmethod
    def branch_for(task_id: int) -> str:
        return f"jarvis/task-{int(task_id)}"

    def prepare(self, task_id: int, recorded=None) -> Workspace:
        if not self.available():
            raise WorkspaceError("workspace isolation requires a Git working tree")
        branch = str((recorded or {}).get("branch") or self.branch_for(task_id))
        path = Path((recorded or {}).get("path") or (self.base / f"task-{int(task_id)}"))
        path = path.resolve()
        if self.root not in path.parents:
            raise WorkspaceError("workspace path is outside the project")
        if path.exists():
            # It must be an actual registered worktree, never an arbitrary folder.
            listed = self._git(["worktree", "list", "--porcelain"]).stdout
            registered = [Path(line.removeprefix("worktree ")).resolve()
                          for line in listed.splitlines() if line.startswith("worktree ")]
            if path not in registered:
                raise WorkspaceError("existing workspace path is not a registered Git worktree")
            return Workspace(task_id, path, branch)
        self.base.mkdir(parents=True, exist_ok=True)
        branches = self._git(["branch", "--format=%(refname:short)"]).stdout.splitlines()
        args = ["worktree", "add"] + ([] if branch in branches else ["-b", branch]) + [str(path), branch if branch in branches else "HEAD"]
        self._git(args)
        return Workspace(task_id, path, branch)

    def checkpoint_and_verify(self, workspace: Workspace) -> str:
        # The worker's interpreter is project-local in normal operation. Tests
        # are intentionally the standard-library suite: no installs or network.
        test = self._run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
                         cwd=str(workspace.path), capture_output=True, text=True, encoding="utf-8", errors="replace")
        if test.returncode:
            raise WorkspaceError("workspace tests failed: " + (test.stderr or test.stdout)[-500:])
        self._git(["add", "-A"], cwd=workspace.path)
        staged = self._git(["diff", "--cached", "--quiet"], cwd=workspace.path, check=False)
        if staged.returncode == 0:
            raise WorkspaceError("task made no tracked changes; no checkpoint created")
        self._git(["commit", "-m", f"Jarvis task {workspace.task_id}"], cwd=workspace.path)
        return self._git(["rev-parse", "HEAD"], cwd=workspace.path).stdout.strip()

    def integrate(self, workspace: Workspace, target="main") -> str:
        lock = tasklock.acquire(self.root / "bridge" / "locks" / "workspace-integrate.lock", owner="workspace")
        if lock is None:
            raise WorkspaceError("another task integration is in progress")
        try:
            if self._git(["status", "--porcelain"], cwd=self.root).stdout.strip():
                raise WorkspaceError("target working tree is not clean; workspace preserved for manual integration")
            current = self._git(["branch", "--show-current"], cwd=self.root).stdout.strip()
            if current != target:
                raise WorkspaceError(f"target checkout is {current or 'detached'}, expected {target}; workspace preserved")
            merge = self._git(["merge", "--no-ff", "--no-edit", workspace.branch], cwd=self.root, check=False)
            if merge.returncode:
                self._git(["merge", "--abort"], cwd=self.root, check=False)
                raise WorkspaceError("merge conflict; target restored and workspace preserved")
            return self._git(["rev-parse", "HEAD"], cwd=self.root).stdout.strip()
        finally:
            lock.release()

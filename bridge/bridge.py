#!/usr/bin/env python3
"""Jarvis task bridge: a file-based mailbox between ChatGPT and Claude Code.

Flow: ChatGPT writes a task -> you paste it in (`add`) -> Claude Code works it and
records progress (`update`) -> `report` prints a paste-ready status for ChatGPT.
Stdlib only, local only, no network, no secrets stored.

  python bridge.py add "title" [--body TEXT | --stdin]
  python bridge.py list
  python bridge.py show ID
  python bridge.py update ID --status pending|in-progress|blocked|done [--note TEXT]
  python bridge.py report [--file PATH ...]
"""
import argparse, json, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = Path(__file__).resolve().parent / "tasks"
STATUSES = ("pending", "in-progress", "blocked", "done")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "tasks", ".idea", ".vscode"}
SECRET_NAMES = re.compile(r"(^\.env|secret|credential|token|\.pem$|\.key$|id_rsa)", re.I)
REDACT = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"(?i:(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*)['\"]?[^\s'\"]{6,})"
)


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def path_for(tid):
    return TASKS / f"{int(tid):03d}.json"


def load(tid):
    p = path_for(tid)
    if not p.exists():
        sys.exit(f"No task {tid}")
    return json.loads(p.read_text(encoding="utf-8"))


def save(t):
    TASKS.mkdir(parents=True, exist_ok=True)
    t["updated"] = now()
    path_for(t["id"]).write_text(json.dumps(t, indent=2), encoding="utf-8")


def all_tasks():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(TASKS.glob("*.json"))]


def redact(text):
    return REDACT.sub("[REDACTED]", text)


def cmd_add(a):
    body = sys.stdin.read() if a.stdin else (a.body or "")
    ids = [t["id"] for t in all_tasks()]
    t = {"id": max(ids, default=0) + 1, "title": a.title, "body": body.strip(),
         "status": "pending", "created": now(), "updated": now(), "notes": []}
    save(t)
    print(f"Added task {t['id']}: {t['title']}")


def cmd_list(_):
    for t in all_tasks():
        print(f"{t['id']:>3}  [{t['status']:<11}] {t['title']}")


def cmd_show(a):
    t = load(a.id)
    print(f"# Task {t['id']}: {t['title']}  [{t['status']}]\n\n{t['body']}\n")
    for n in t["notes"]:
        print(f"- {n['at']}: {n['text']}")


def cmd_update(a):
    t = load(a.id)
    if a.status:
        t["status"] = a.status
    if a.note:
        t["notes"].append({"at": now(), "text": redact(a.note)})
    save(t)
    print(f"Task {t['id']} -> {t['status']}")


def tree(root, depth=3):
    lines = []

    def walk(d, level):
        if level > depth:
            return
        for p in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if p.name in SKIP_DIRS or SECRET_NAMES.search(p.name):
                continue
            lines.append("  " * level + p.name + ("/" if p.is_dir() else ""))
            if p.is_dir():
                walk(p, level + 1)

    walk(root, 0)
    return lines[:200]


def cmd_report(a):
    out = [f"# Jarvis status report ({now()})", "", "## Tasks"]
    ts = all_tasks()
    out += [f"- #{t['id']} [{t['status']}] {t['title']}" for t in ts] or ["- (none)"]
    for t in ts:
        if t["notes"]:
            out += ["", f"### Task {t['id']} notes"]
            out += [f"- {n['at']}: {n['text']}" for n in t["notes"][-5:]]
    out += ["", "## File tree", "```"] + tree(ROOT) + ["```"]
    for f in a.file or []:
        p = (ROOT / f).resolve()
        if ROOT not in p.parents and p != ROOT:
            out += ["", f"## {f}", "(refused: outside project)"]
        elif SECRET_NAMES.search(p.name) or not p.is_file():
            out += ["", f"## {f}", "(refused or not found)"]
        else:
            out += ["", f"## {f}", "```", redact(p.read_text(encoding="utf-8", errors="replace"))[:20000], "```"]
    print("\n".join(out))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add"); s.add_argument("title"); s.add_argument("--body"); s.add_argument("--stdin", action="store_true"); s.set_defaults(f=cmd_add)
    sub.add_parser("list").set_defaults(f=cmd_list)
    s = sub.add_parser("show"); s.add_argument("id", type=int); s.set_defaults(f=cmd_show)
    s = sub.add_parser("update"); s.add_argument("id", type=int); s.add_argument("--status", choices=STATUSES); s.add_argument("--note"); s.set_defaults(f=cmd_update)
    s = sub.add_parser("report"); s.add_argument("--file", action="append"); s.set_defaults(f=cmd_report)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

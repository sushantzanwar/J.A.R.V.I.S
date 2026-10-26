"""Store or remove a secret in the encrypted Jarvis vault without it ever appearing on screen or in chat.

    .venv\\Scripts\\python.exe scripts\\jarvis_secret.py set google_api_key
    .venv\\Scripts\\python.exe scripts\\jarvis_secret.py set search_cx
    .venv\\Scripts\\python.exe scripts\\jarvis_secret.py list      (names only, never values)
    .venv\\Scripts\\python.exe scripts\\jarvis_secret.py delete google_api_key
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.core.security import Vault  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("set", "list", "delete"):
        print(__doc__)
        return 2
    vault = Vault()
    if argv[0] == "list":
        print("\n".join(vault.keys()) or "(vault is empty)")
        return 0
    if len(argv) < 2:
        print("name required")
        return 2
    name = argv[1]
    if argv[0] == "delete":
        print("deleted" if vault.delete(name) else "no such secret")
        return 0
    value = getpass.getpass(f"Value for {name} (hidden, then Enter): ").strip()
    if not value:
        print("nothing entered; nothing stored")
        return 1
    vault.set(name, value)
    print(f"stored {name} ({len(value)} characters) in the encrypted vault")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

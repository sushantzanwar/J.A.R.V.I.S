# Integrations, permissions and credentials

## Google (Gmail, Calendar, Drive)

One-time setup. Only you can do it, because Google issues the OAuth client:

1. console.cloud.google.com -> new project -> enable **Gmail API**, **Google Calendar API**, **Google Drive API**.
2. OAuth consent screen -> External -> add your own Google account as a test user.
3. Credentials -> Create credentials -> OAuth client ID -> **Desktop app** -> download the JSON.
4. Save it as `%LOCALAPPDATA%\Jarvis\google_client_secret.json` (or point `JARVIS_GOOGLE_CLIENT_SECRET` at it).
5. Say **"connect google"** and approve the consent screen in the browser.

The token is stored encrypted in the vault, never as a plain file. Scopes are the narrowest that work:
Gmail read + send (no delete), Calendar events, Drive read-only.

| Say | Does |
|---|---|
| check my email / any new emails | unread inbox, sender and subject |
| search my email for invoices | Gmail search |
| what's on my calendar (tomorrow / this week) | agenda |
| search drive for tax return / find budget in my drive | Drive file names |
| email bob ... / add a meeting to my calendar ... | planned by the model, **asks you first** |

## Permissions

Every tool declares a risk. `low` runs at once. `consequential` (sending email, creating events, anything
that reaches other people or accounts) pauses and says what it is about to do; **"yes"** runs it, **"no"**
cancels. A waiting confirmation survives a restart.

Local PC actions, including shutdown, sleep and sign-out, are `low` by the owner's decision.

Override any tool in `%LOCALAPPDATA%\Jarvis\permissions.json`:

```json
{"tools": {"gmail_send": "allow", "calendar_create": "deny"}}
```

Levels: `allow` (never ask), `confirm` (always ask), `deny` (never run, even if a plan asks).

## Audit log and vault

- `%LOCALAPPDATA%\Jarvis\actions.jsonl`: one line per action (tool, redacted arguments, allow / approved /
  confirm / deny, outcome, task id). Rotates at 5 MB. Logging never raises.
- `%LOCALAPPDATA%\Jarvis\vault.bin`: JSON encrypted with Windows DPAPI, bound to your Windows account.
  Copying the file to another machine or account makes it unreadable.

## Adding an integration

Subclass `Service` in `jarvis/integrations/services/`, return `Tool`s (mark anything that affects other
people `risk="consequential"`), add plain phrases in `resolve()`, and list it in `load_services()`.
The agent, permissions, confirmation flow and audit log then apply with no further wiring.

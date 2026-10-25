"""Google: Gmail, Calendar and Drive through the official APIs.

Setup is one time, and only the user can do it because Google issues the OAuth client:
  1. console.cloud.google.com -> new project -> enable Gmail API, Google Calendar API, Drive API
  2. APIs & Services -> Credentials -> Create OAuth client ID -> "Desktop app" -> download JSON
  3. save it as  %LOCALAPPDATA%\\Jarvis\\google_client_secret.json
  4. say "connect google" and approve the consent screen in the browser
The resulting token is stored encrypted in the vault (Windows DPAPI), never as a plain file.

Scopes are the narrowest that do the job: Gmail read and send (no delete), Calendar events,
Drive read-only. Sending mail and creating events are 'consequential': Jarvis asks first.
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from jarvis.core.agent.tools import Tool
from jarvis.core.security.vault import Vault, VaultError
from jarvis.integrations.services.base import Service

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
]
TOKEN_KEY = "google_token"
SECRET_ENV = "JARVIS_GOOGLE_CLIENT_SECRET"
MAX_RESULTS = 5
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

NOT_CONNECTED = ("Could not use Google: it is not connected yet. Say 'connect google' (one-time setup is described "
                 "in jarvis/integrations/services/google.py).")


def default_secret_path() -> Path:
    override = os.environ.get(SECRET_ENV)
    if override:
        return Path(override)
    from jarvis.core.store import default_data_dir
    return default_data_dir() / "google_client_secret.json"


class GoogleService(Service):
    name = "google"
    description = "Gmail, Google Calendar and Google Drive"

    def __init__(self, vault: Vault | None = None, *, build=None, secret_path: Path | str | None = None,
                 flow_runner=None, now=datetime.now) -> None:
        self.vault = vault
        self._build = build               # injectable: tests pass a fake API factory
        self._secret_path = Path(secret_path) if secret_path else None
        self._flow_runner = flow_runner   # injectable: tests skip the browser consent screen
        self._now = now

    # ---- credentials ------------------------------------------------------------------
    def _token(self) -> dict | None:
        if self.vault is None:
            return None
        try:
            return self.vault.get(TOKEN_KEY)
        except VaultError:
            return None

    def configured(self) -> bool:
        return self._token() is not None

    def _credentials(self):
        from google.oauth2.credentials import Credentials
        data = self._token()
        if not data:
            return None
        creds = Credentials.from_authorized_user_info(data, SCOPES)
        if not creds.valid and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            self.vault.set(TOKEN_KEY, json.loads(creds.to_json()))      # keep the refreshed token
        return creds

    def connect(self) -> str:
        if self.vault is None:
            return "Google cannot be connected: no credential vault is available in this session."
        path = self._secret_path or default_secret_path()
        if not path.is_file():
            return (f"I need your Google OAuth client file first. Save it as {path} "
                    "(Google Cloud Console -> Credentials -> OAuth client ID -> Desktop app), then say 'connect google'.")
        try:
            if self._flow_runner is not None:
                token = self._flow_runner(path, SCOPES)
            else:
                from google_auth_oauthlib.flow import InstalledAppFlow
                flow = InstalledAppFlow.from_client_secrets_file(str(path), SCOPES)
                token = json.loads(flow.run_local_server(port=0, open_browser=True).to_json())
            self.vault.set(TOKEN_KEY, token)
        except Exception as e:
            return f"Could not connect Google: {type(e).__name__}: {str(e)[:120]}"
        return "Google connected. Gmail, Calendar and Drive are ready."

    def _api(self, name: str, version: str):
        if self._build is not None:
            return self._build(name, version)
        creds = self._credentials()
        if creds is None:
            return None
        from googleapiclient.discovery import build
        return build(name, version, credentials=creds, cache_discovery=False)

    def _call(self, api: str, version: str, fn) -> str:
        """Run fn(service) with uniform not-connected and API-error reporting."""
        try:
            svc = self._api(api, version)
        except Exception as e:
            return f"Could not reach Google: {type(e).__name__}: {str(e)[:100]}"
        if svc is None:
            return NOT_CONNECTED
        try:
            return fn(svc)
        except Exception as e:
            return f"Could not complete that with Google: {type(e).__name__}: {str(e)[:120]}"

    # ---- Gmail ------------------------------------------------------------------------
    def gmail_search(self, query: str) -> str:
        def run(svc):
            msgs = svc.users().messages().list(userId="me", q=query, maxResults=MAX_RESULTS).execute().get("messages", [])
            if not msgs:
                return f"No emails found for '{query}'."
            lines = []
            for m in msgs:
                d = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                               metadataHeaders=["From", "Subject", "Date"]).execute()
                h = {x["name"].lower(): x["value"] for x in d.get("payload", {}).get("headers", [])}
                sender = re.sub(r"<.*?>", "", h.get("from", "unknown")).strip().strip('"') or "unknown"
                lines.append(f"{sender}: {h.get('subject', '(no subject)')}")
            return f"{len(lines)} email(s): " + "; ".join(lines) + "."
        return self._call("gmail", "v1", run)

    def gmail_send(self, to: str, subject: str, body: str) -> str:
        if not _EMAIL.match(to.strip()):
            return f"Invalid email address '{to}'; nothing was sent."

        def run(svc):
            msg = EmailMessage()
            msg["To"], msg["Subject"] = to.strip(), subject
            msg.set_content(body)
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            return f"Sent email to {to.strip()} (id {sent.get('id', '?')})."
        return self._call("gmail", "v1", run)

    # ---- Calendar ---------------------------------------------------------------------
    def calendar_agenda(self, days: str) -> str:
        try:
            n = max(1, min(int(float(days)), 31))
        except (TypeError, ValueError):
            return f"Invalid number of days '{days}'."

        def run(svc):
            start = self._now().astimezone()
            end = start + timedelta(days=n)
            items = svc.events().list(calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
                                      singleEvents=True, orderBy="startTime", maxResults=10).execute().get("items", [])
            if not items:
                return "Nothing on your calendar" + (" today." if n == 1 else f" in the next {n} days.")
            lines = []
            for ev in items:
                when = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
                try:
                    when = datetime.fromisoformat(when).strftime("%a %H:%M") if "T" in when else when
                except ValueError:
                    pass
                lines.append(f"{when} {ev.get('summary', '(no title)')}")
            return f"{len(lines)} event(s): " + "; ".join(lines) + "."
        return self._call("calendar", "v3", run)

    def calendar_create(self, title: str, start: str, minutes: str) -> str:
        try:
            begin = datetime.fromisoformat(start.strip().replace(" ", "T", 1))
            length = max(5, min(int(float(minutes)), 24 * 60))
        except (TypeError, ValueError):
            return f"Invalid start time '{start}' (use e.g. 2026-10-27 15:00) or length '{minutes}'; nothing was added."
        if begin.tzinfo is None:
            begin = begin.astimezone()           # interpret as local time
        end = begin + timedelta(minutes=length)

        def run(svc):
            ev = svc.events().insert(calendarId="primary", body={
                "summary": title, "start": {"dateTime": begin.isoformat()}, "end": {"dateTime": end.isoformat()},
            }).execute()
            return f"Added '{title}' to your calendar at {begin.strftime('%a %d %b %H:%M')} (id {ev.get('id', '?')})."
        return self._call("calendar", "v3", run)

    # ---- Drive ------------------------------------------------------------------------
    def drive_search(self, query: str) -> str:
        safe = query.replace("\\", "\\\\").replace("'", "\\'")

        def run(svc):
            files = svc.files().list(q=f"name contains '{safe}' and trashed=false", pageSize=MAX_RESULTS,
                                     fields="files(id,name,mimeType,modifiedTime)").execute().get("files", [])
            if not files:
                return f"No Drive files found matching '{query}'."
            return f"{len(files)} file(s): " + "; ".join(f["name"] for f in files) + "."
        return self._call("drive", "v3", run)

    # ---- Service interface ------------------------------------------------------------
    def tools(self) -> list[Tool]:
        return [
            Tool("google_connect", "Connect Jarvis to the user's Google account (opens a browser consent screen).",
                 self.connect, retryable=False),
            Tool("gmail_search", "Search or list emails, e.g. 'is:unread' or 'from:alice'.",
                 self.gmail_search, {"query": "Gmail search query"}, kind="query"),
            Tool("gmail_send", "Send an email.", self.gmail_send,
                 {"to": "recipient address", "subject": "subject line", "body": "message text"},
                 risk="consequential", retryable=False,
                 describe=lambda a: f"send an email to {a.get('to')} with the subject '{a.get('subject')}'"),
            Tool("calendar_agenda", "List upcoming calendar events.", self.calendar_agenda,
                 {"days": "how many days ahead, e.g. 1 for today"}, kind="query"),
            Tool("calendar_create", "Add an event to the calendar.", self.calendar_create,
                 {"title": "event title", "start": "start time like 2026-10-27 15:00", "minutes": "length in minutes"},
                 risk="consequential", retryable=False,
                 describe=lambda a: f"add '{a.get('title')}' to your calendar at {a.get('start')}"),
            Tool("drive_search", "Search Google Drive file names.", self.drive_search,
                 {"query": "text in the file name"}, kind="query"),
        ]

    _CHECK = re.compile(r"^(?:check|read|show|open|any|do i have)(?:\s+(?:me|my|the|new|unread))*\s*(?:e-?mails?|inbox|mail|gmail)\b")
    _SEARCH_MAIL = re.compile(r"^(?:search|find|look\s+up)\s+(?:my\s+)?(?:e-?mails?|gmail|inbox|mail)\s+for\s+(.+)$")
    _AGENDA = re.compile(r"^(?:what(?:'s| is)?\s+on\s+my\s+(?:calendar|schedule|agenda)|what\s+do\s+i\s+have|"
                         r"(?:show|read|check)\s+(?:me\s+)?my\s+(?:calendar|schedule|agenda)|my\s+(?:calendar|schedule|agenda))"
                         r"(?:\s+(?:for\s+)?(today|tomorrow|this\s+week|next\s+week|this\s+month))?\s*$")
    _DRIVE = re.compile(r"^(?:search|find|look\s+up)\s+(?:my\s+)?(?:google\s+)?drive\s+for\s+(.+)$"
                        r"|^(?:search|find)\s+(.+?)\s+(?:in|on)\s+(?:my\s+)?(?:google\s+)?drive$")
    _CONNECT = re.compile(r"^(?:connect|link|set\s*up|sign\s+in\s+to)\s+(?:to\s+)?(?:my\s+)?google(?:\s+account)?$")

    def resolve(self, text: str):
        q = text.strip().lower().rstrip(" .!?,;")
        if self._CONNECT.match(q):
            return "google_connect", {}
        if self._CHECK.match(q):
            return "gmail_search", {"query": "is:unread in:inbox"}
        m = self._SEARCH_MAIL.match(q)
        if m:
            return "gmail_search", {"query": m.group(1).strip()}
        m = self._AGENDA.match(q)
        if m:
            span = (m.group(1) or "today").replace(" ", "")
            return "calendar_agenda", {"days": {"today": "1", "tomorrow": "2", "thisweek": "7",
                                                "nextweek": "14", "thismonth": "31"}.get(span, "1")}
        m = self._DRIVE.match(q)
        if m:
            return "drive_search", {"query": (m.group(1) or m.group(2)).strip()}
        return None

#!/usr/bin/env python3
"""
Dashboard Update — Script autonome

Collecte :
  - Google Calendar et Gmail via gws CLI ;
  - Zimbra (messagerie UL) via IMAP (zimbra_unread.fetch_zimbra_mailbox).

Écrit le résultat dans dashboard.json, puis lance summarize_mail.py si un
dernier mail (Gmail ou Zimbra) n'a pas encore de résumé OpenClaw.

Lancé toutes les 2 min par menubar.py (ou manuellement pour test).
"""

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from gws_errors import derive_gws_auth_status
from load_env import load_project_env
from zimbra_unread import fetch_zimbra_mailbox

load_project_env()

DATA_FILE: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.json")
MAX_EVENTS: int = 2
MAX_UNREAD: int = 100

def _gws_env() -> dict:
    env: dict = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
    env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = os.path.expanduser("~/.config/gws")
    return env

def _extract_text_body(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        nested = _extract_text_body(part)
        if nested:
            return nested
    return ""


def main() -> None:
    env: dict = _gws_env()
    now: datetime = datetime.now(timezone.utc)
    now_local: str = now.astimezone().isoformat(timespec="seconds")
    now_utc: str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max: str = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Données précédentes : on s'y replie si un appel échoue, pour éviter
    # d'écraser de bonnes données par du vide en cas de timeout réseau.
    previous: dict = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                previous = json.load(f)
        except (OSError, json.JSONDecodeError):
            previous = {}

    # ── Calendar ──
    next_events: Optional[list] = None
    cal_ok: bool = False
    cal: Optional[subprocess.CompletedProcess[str]] = None
    try:
        cal = subprocess.run(
            ["gws", "calendar", "events", "list",
             "--params", json.dumps({
                 "calendarId": "primary",
                 "timeMin": now_utc,
                 "timeMax": time_max,
                 "singleEvents": True,
                 "orderBy": "startTime",
                 "maxResults": MAX_EVENTS,
             })],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if cal.returncode == 0:
            cal_ok = True
            next_events = []
            for ev in json.loads(cal.stdout).get("items", []):
                next_events.append({
                    "title": ev.get("summary", ""),
                    "start": (ev.get("start") or {}).get("dateTime", ""),
                    "end": (ev.get("end") or {}).get("dateTime", ""),
                    "location": ev.get("location", ""),
                })
    except Exception as e:
        print(f"Calendar error: {e}", file=sys.stderr)
    if next_events is None:  # appel échoué → on garde les événements précédents
        next_events = previous.get("next_events") or []

    # ── Gmail : count ──
    count_ok: bool = False
    unread_gmail: int = 0
    first_msg_id: Optional[str] = None
    gmail: Optional[subprocess.CompletedProcess[str]] = None
    try:
        gmail = subprocess.run(
            ["gws", "gmail", "users", "messages", "list",
             "--params", json.dumps({
                 "userId": "me",
                 "labelIds": ["INBOX"],
                 "q": "is:unread",
                 "maxResults": MAX_UNREAD,
             })],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if gmail.returncode == 0:
            count_ok = True
            messages: list = json.loads(gmail.stdout).get("messages", [])
            unread_gmail = len(messages)
            if messages:
                first_msg_id = messages[0].get("id")
    except Exception as e:
        print(f"Gmail list error: {e}", file=sys.stderr)

    # Invariant : gmail_status reflète la réussite du list, pas du get détail.
    gmail_status: str = "ok" if count_ok else "error"

    gws_auth_status: str = derive_gws_auth_status(
        cal_ok=cal_ok,
        gmail_count_ok=count_ok,
        cal_proc=cal,
        gmail_proc=gmail,
        previous_status=str(previous.get("gws_auth_status", "ok")),
    )

    # ── Gmail : latest ──
    latest_unread = None
    if not count_ok:
        # Compte indisponible (timeout/auth) : on préserve l'état précédent
        # en entier plutôt que d'afficher 0 mail non lu à tort.
        unread_gmail = int(previous.get("unread_gmail", 0))
        latest_unread = previous.get("latest_unread")
    elif first_msg_id:
        prev_latest: dict = previous.get("latest_unread") or {}
        try:
            msg: subprocess.CompletedProcess = subprocess.run(
                ["gws", "gmail", "users", "messages", "get",
                 "--params", json.dumps({
                     "userId": "me",
                     "id": first_msg_id,
                     "format": "full",
                 })],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if msg.returncode == 0:
                msg_data: dict = json.loads(msg.stdout)
                headers: list = msg_data.get("payload", {}).get("headers", [])
                from_val: str = ""
                subject_val: str = ""
                for h in headers:
                    name: str = h.get("name", "").lower()
                    if name == "from":
                        from_val = h.get("value", "")
                    elif name == "subject":
                        subject_val = h.get("value", "")[:80]
                snippet: str = msg_data.get("snippet", "")
                body: str = _extract_text_body(msg_data.get("payload", {}))
                latest_unread = {
                    "id": first_msg_id,
                    "from": from_val,
                    "subject": subject_val,
                    "snippet": snippet,
                    "body": body,
                }
                # Même mail qu'avant : on garde le résumé déjà calculé.
                if prev_latest.get("id") == first_msg_id and prev_latest.get("summary"):
                    latest_unread["summary"] = prev_latest["summary"]
            elif prev_latest.get("id") == first_msg_id:
                # get échoué mais c'est le même mail : on garde l'ancien.
                latest_unread = prev_latest
        except Exception as e:
            print(f"Gmail get error: {e}", file=sys.stderr)
            if prev_latest.get("id") == first_msg_id:
                latest_unread = prev_latest

    # ── Zimbra (IMAP) ──
    # Même philosophie que Gmail : en cas d'échec (login, réseau, IMAP off),
    # on préserve l'état précédent plutôt que d'afficher 0 à tort.
    z_user: Optional[str] = os.environ.get("ZIMBRA_USER")
    z_pass: Optional[str] = os.environ.get("ZIMBRA_PASS")
    unread_zimbra: int = int(previous.get("unread_zimbra", 0))
    latest_unread_zimbra = previous.get("latest_unread_zimbra")
    # disabled = pas de compte configuré ; ok/error = tentative IMAP réelle.
    zimbra_status: str = "disabled"
    if z_user and z_pass:
        prev_z: dict = previous.get("latest_unread_zimbra") or {}
        try:
            z_count, z_latest = fetch_zimbra_mailbox(z_user, z_pass)
            unread_zimbra = z_count
            zimbra_status = "ok"
            if z_latest is None:
                latest_unread_zimbra = None
            else:
                z_dict: dict = {
                    "id": z_latest["id"],
                    "from": z_latest["from_"],
                    "subject": z_latest["subject"],
                    "snippet": z_latest["snippet"],
                    "body": z_latest["body"],
                }
                # Même mail qu'avant : on garde le résumé déjà calculé.
                if prev_z.get("id") == z_latest["id"] and prev_z.get("summary"):
                    z_dict["summary"] = prev_z["summary"]
                latest_unread_zimbra = z_dict
        except Exception as e:
            print(f"Zimbra error: {e}", file=sys.stderr)
            zimbra_status = "error"
            # fallback : valeurs précédentes déjà en place.

    # ── Write JSON ──
    dashboard: dict = {
        "next_events": next_events,
        "unread_gmail": unread_gmail,
        "gmail_status": gmail_status,
        "gws_auth_status": gws_auth_status,
        "latest_unread": latest_unread,
        "unread_zimbra": unread_zimbra,
        "zimbra_status": zimbra_status,
        "latest_unread_zimbra": latest_unread_zimbra,
        "last_updated": now_local,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(dashboard, f, ensure_ascii=False)

    print(f"OK — {len(next_events)} events, {unread_gmail} gmail, "
          f"{unread_zimbra} zimbra, {now_local}")

    # ── Summarize latest mail via OpenClaw (Gmail + Zimbra) ──
    # On lance summarize_mail.py si l'un des deux derniers mails a un corps
    # mais pas encore de résumé. Le script boucle sur les deux côtés.
    def _needs_summary(m) -> bool:
        return (isinstance(m, dict)
                and not m.get("summary")
                and bool(m.get("body") or m.get("snippet")))

    if _needs_summary(latest_unread) or _needs_summary(latest_unread_zimbra):
        script: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "summarize_mail.py")
        try:
            subprocess.run(
                [sys.executable, script],
                timeout=60,
            )
        except Exception as e:
            print(f"Summarize error: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()

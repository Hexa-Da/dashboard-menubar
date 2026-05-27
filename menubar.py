#!/usr/bin/env python3
"""
Dashboard Menubar

Affiche dans la barre de menus macOS :
  - le prochain événement Google Calendar ;
  - le nombre de mails Gmail non lus + détails du dernier ;
  - les actions : rafraîchir, marquer Gmail comme lu (UI seulement),
    forcer une mise à jour, quitter.

Les données sont lues depuis ~/.openclaw/workspace/agent_dashboard.json
(écrit toutes les 10 min par dashboard_update.py via LaunchAgent).

Prérequis : `pip install rumps pyobjc-framework-Cocoa`.
"""

import base64
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Callable, Optional

import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

DATA_FILE: str = os.path.expanduser("~/.openclaw/workspace/agent_dashboard.json")
REFRESH_INTERVAL: int = 10  # secondes entre deux relectures du JSON

_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
ICON_BELL: str = os.path.join(_SCRIPT_DIR, "assets", "bell.png")
ICON_MENUBAR_SIZE: tuple[float, float] = (18.0, 18.0)

URL_GOOGLE_CALENDAR: str = "https://calendar.google.com/calendar/u/0/r"
URL_GMAIL: str = "https://mail.google.com/mail/u/0/#inbox"

_WEEKDAYS_FR: tuple[str, ...] = (
    "Lundi", "Mardi", "Mercredi", "Jeudi",
    "Vendredi", "Samedi", "Dimanche",
)


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────


def _hide_dock_icon() -> None:
    """Masque l'icône Python dans le Dock ; l'icône menubar reste visible."""
    ns_app: NSApplication = NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)


def _open_in_browser(url: str) -> Callable[[object], None]:
    """Retourne un callback rumps qui ouvre `url` dans le navigateur par défaut."""

    def _handler(_: object) -> None:
        subprocess.run(["open", url], check=False)

    return _handler


def send_notification(title: str, message: str, subtitle: str = "") -> None:
    """Envoie une notification macOS native via osascript."""
    script: str = (
        f'display notification "{message}" '
        f'with title "{title}" subtitle "{subtitle}"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True)


def _parse_local_datetime(iso_str: str) -> datetime:
    """ISO 8601 → datetime naïf en heure locale (évite aware/naïf mixés)."""
    dt: datetime = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def format_event_time(start_str: str, end_str: str) -> str:
    """Formate « Lundi • 14:00–15:00 ».

    Préconditions : start_str non vide.
    Invariant : retourne start_str brut si le parsing échoue.
    """
    try:
        start: datetime = _parse_local_datetime(start_str)
        day_label: str = _WEEKDAYS_FR[start.weekday()]
        if end_str.strip():
            end: datetime = _parse_local_datetime(end_str)
            times: str = f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        else:
            times = start.strftime("%H:%M")
        return f"{day_label} • {times}"
    except (ValueError, TypeError):
        return start_str


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


def _format_mail_sender(from_header: str) -> str:
    """Extrait le nom affiché d'un en-tête `From:` (sans `<email@…>`)."""
    raw: str = from_header.strip()
    if not raw:
        return "—"
    display_name, email_addr = parseaddr(raw)
    display_name = display_name.strip()
    if display_name:
        return display_name
    if email_addr:
        return email_addr
    return raw


def _extract_event(data: dict) -> Optional[dict]:
    """Renvoie le prochain événement pertinent (ou None).

    Ignore un événement dont la fin est dans moins de 30 minutes.
    Accepte `next_events` (liste) et `next_event` (objet legacy).
    """
    now: datetime = datetime.now()
    cutoff: timedelta = timedelta(minutes=30)

    candidates: list[dict] = []
    events: object = data.get("next_events")
    if isinstance(events, list):
        candidates = [e for e in events if isinstance(e, dict)]
    else:
        legacy: object = data.get("next_event")
        if isinstance(legacy, dict):
            candidates = [legacy]

    for e in candidates:
        end_str: str = str(e.get("end", ""))
        if not end_str.strip():
            return e
        try:
            end_local: datetime = _parse_local_datetime(end_str)
            if end_local - cutoff > now:
                return e
        except (ValueError, TypeError):
            return e
    return None


def load_data() -> dict:
    """Charge le JSON dashboard. Renvoie au minimum un dict (potentiellement
    vide si le fichier est absent ou invalide)."""
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ─────────────────────────────────────────
# App
# ─────────────────────────────────────────


class DashboardMenubar(rumps.App):
    """App rumps qui peuple la barre de menus avec les données dashboard."""

    def __init__(self) -> None:
        super().__init__(
            "Dashboard",
            title="",
            icon=ICON_BELL,
            template=True,
            quit_button=None,
        )
        if self._icon_nsimage is not None:
            self._icon_nsimage.setSize_(ICON_MENUBAR_SIZE)

        # État interne
        self._prev_unread_total: int = 0
        self._prev_event_title: Optional[str] = None
        self._gmail_cleared: bool = False
        self._cleared_at_unread: int = 0
        self._last_known_unread: int = 0

        # ── Événement ──────────────────────────────────
        self.event_title = rumps.MenuItem(
            "📅 Chargement...", callback=_open_in_browser(URL_GOOGLE_CALENDAR)
        )
        self.event_time = rumps.MenuItem(
            "   ⏰ —", callback=_open_in_browser(URL_GOOGLE_CALENDAR)
        )
        self.event_location = rumps.MenuItem(
            "   📍 —", callback=_open_in_browser(URL_GOOGLE_CALENDAR)
        )

        # ── Gmail ──────────────────────────────────────
        self.mail_gmail = rumps.MenuItem(
            "✉️ Gmail : —", callback=_open_in_browser(URL_GMAIL)
        )
        self.mail_from = rumps.MenuItem(
            "   👤 —", callback=_open_in_browser(URL_GMAIL)
        )
        self.mail_subject = rumps.MenuItem(
            "   ✏️ —", callback=_open_in_browser(URL_GMAIL)
        )

        # ── Actions ────────────────────────────────────
        self.refresh_btn = rumps.MenuItem(
            "Dernière mise à jour : —", callback=self.manual_refresh
        )
        self.mail_clear_btn = rumps.MenuItem(
            "Marquer les mails comme lus", callback=self.clear_gmail_local
        )
        self.force_update_btn = rumps.MenuItem(
            "Forcer la mise à jour", callback=self.force_update
        )
        self.quit_btn = rumps.MenuItem("Quitter", callback=rumps.quit_application)

        self.menu = [
            self.event_title,
            self.event_time,
            self.event_location,
            None,
            self.mail_gmail,
            self.mail_from,
            self.mail_subject,
            None,
            self.refresh_btn,
            self.mail_clear_btn,
            self.force_update_btn,
            None,
            self.quit_btn,
        ]

        self._start_refresh_thread()

    # ─────────────────────────────────────────
    # Actions
    # ─────────────────────────────────────────

    def _start_refresh_thread(self) -> None:
        """Démarre une boucle de fond qui relit le JSON toutes les
        REFRESH_INTERVAL secondes."""

        def loop() -> None:
            while True:
                self.refresh_data()
                time.sleep(REFRESH_INTERVAL)

        threading.Thread(target=loop, daemon=True).start()

    def manual_refresh(self, _: object) -> None:
        """Relecture manuelle du JSON depuis le menu."""
        threading.Thread(target=self.refresh_data, daemon=True).start()

    def clear_gmail_local(self, _: object) -> None:
        """Marque les mails comme « lus » côté interface seulement.

        Effets : flag `_gmail_cleared` activé + seuil mémorisé. Tant qu'aucun
        nouveau mail (au-delà du seuil) n'arrive, l'UI affiche 0.
        """
        self._gmail_cleared = True
        self._cleared_at_unread = self._last_known_unread
        self._prev_unread_total = self._last_known_unread
        self.mail_gmail.title = "✉️ Gmail : 0 non lu"
        self.mail_from.title = "   👤 —"
        self.mail_subject.title = "   ✏️ —"
        self.title = ""

    def force_update(self, _: object) -> None:
        """Appelle les APIs Google via gws et réécrit le JSON."""

        def _run() -> None:
            self.force_update_btn.title = "Mise à jour en cours..."
            gws_env: dict = os.environ.copy()
            gws_env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + gws_env.get("PATH", "")
            gws_env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
            gws_env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = os.path.expanduser("~/.config/gws")

            try:
                now: datetime = datetime.now(timezone.utc)
                now_local: str = now.astimezone().isoformat(timespec="seconds")
                now_utc: str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                time_max: str = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

                # ── Calendar ──
                cal_result = subprocess.run(
                    [
                        "gws", "calendar", "events", "list",
                        "--params", json.dumps({
                            "calendarId": "primary",
                            "timeMin": now_utc,
                            "timeMax": time_max,
                            "singleEvents": True,
                            "orderBy": "startTime",
                            "maxResults": 2,
                        }),
                    ],
                    capture_output=True, text=True, timeout=30, env=gws_env,
                )
                next_events: list = []
                if cal_result.returncode == 0:
                    cal_data: dict = json.loads(cal_result.stdout)
                    for ev in cal_data.get("items", []):
                        next_events.append({
                            "title": ev.get("summary", ""),
                            "start": (ev.get("start") or {}).get("dateTime", ""),
                            "end": (ev.get("end") or {}).get("dateTime", ""),
                            "location": ev.get("location", ""),
                        })

                # ── Gmail : count ──
                gmail_result = subprocess.run(
                    [
                        "gws", "gmail", "users", "messages", "list",
                        "--params", json.dumps({
                            "userId": "me",
                            "labelIds": ["INBOX"],
                            "q": "is:unread",
                            "maxResults": 100,
                        }),
                    ],
                    capture_output=True, text=True, timeout=30, env=gws_env,
                )
                unread_gmail: int = 0
                first_msg_id: Optional[str] = None
                if gmail_result.returncode == 0:
                    gmail_data: dict = json.loads(gmail_result.stdout)
                    messages: list = gmail_data.get("messages", [])
                    unread_gmail = len(messages)
                    if messages:
                        first_msg_id = messages[0].get("id")

                # ── Gmail : latest ──
                latest_unread = None
                if first_msg_id:
                    msg_result = subprocess.run(
                        [
                            "gws", "gmail", "users", "messages", "get",
                            "--params", json.dumps({
                                "userId": "me",
                                "id": first_msg_id,
                                "format": "full",
                            }),
                        ],
                        capture_output=True, text=True, timeout=30, env=gws_env,
                    )
                    if msg_result.returncode == 0:
                        msg_data: dict = json.loads(msg_result.stdout)
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
                            "from": from_val,
                            "subject": subject_val,
                            "snippet": snippet,
                            "body": body,
                        }

                # ── Write JSON ──
                dashboard: dict = {
                    "next_events": next_events,
                    "unread_gmail": unread_gmail,
                    "latest_unread": latest_unread,
                    "last_updated": now_local,
                }
                with open(DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(dashboard, f, ensure_ascii=False)

                self.refresh_data()
                send_notification(
                    title="✅ Dashboard mis à jour",
                    message=f"Gmail: {unread_gmail} · Cal: {len(next_events)} événement{'s' if len(next_events) > 1 else ''}",
                )
            except Exception as exc:
                send_notification("⚠️ Erreur mise à jour", str(exc)[:150])
            finally:
                self.force_update_btn.title = "Forcer la mise à jour"

        threading.Thread(target=_run, daemon=True).start()

    # ─────────────────────────────────────────
    # Rendu / notifications
    # ─────────────────────────────────────────

    def refresh_data(self) -> None:
        """Charge le JSON et met à jour le menu + notifications."""
        data: dict = load_data()
        self._update_menu(data)
        self._check_notifications(data)

    def _update_menu(self, data: dict) -> None:
        """Met à jour les libellés des MenuItems depuis `data`."""
        # ── Événement ─────────────────────────────────
        event: Optional[dict] = _extract_event(data)
        if event is None:
            self.event_title.title = "📅 Aucun événement à venir"
            self.event_time.title = "   ⏰ —"
            self.event_location.title = "   📍 —"
        else:
            title: str = str(event.get("title", "Événement sans titre"))
            self.event_title.title = f"📅 {title}"

            start: str = str(event.get("start", ""))
            end: str = str(event.get("end", ""))
            if start:
                self.event_time.title = f"   ⏰ {format_event_time(start, end)}"
            else:
                self.event_time.title = "   ⏰ Heure inconnue"

            location: str = str(event.get("location", ""))
            self.event_location.title = (
                f"   📍 {location}" if location else "   📍 Pas de lieu"
            )

        # ── Gmail ─────────────────────────────────────
        gmail_raw: int = int(data.get("unread_gmail", 0))
        if self._gmail_cleared and gmail_raw > self._cleared_at_unread:
            self._gmail_cleared = False
        self._last_known_unread = gmail_raw
        gmail_shown: int = 0 if self._gmail_cleared else gmail_raw

        self.mail_gmail.title = (
            f"✉️ Gmail : {gmail_shown} non lu{'s' if gmail_shown > 1 else ''}"
        )

        latest: object = data.get("latest_unread")
        if isinstance(latest, dict) and gmail_shown > 0:
            sender: str = _format_mail_sender(str(latest.get("from", "")))
            subject: str = str(latest.get("subject", "")).strip() or "(sans objet)"
            if len(sender) > 60:
                sender = sender[:57] + "…"
            if len(subject) > 70:
                subject = subject[:67] + "…"
            self.mail_from.title = f"   👤 {sender}"
            self.mail_subject.title = f"   ✏️ {subject}"
        else:
            self.mail_from.title = "   👤 —"
            self.mail_subject.title = "   ✏️ —"

        self.title = str(gmail_shown) if gmail_shown > 0 else ""
        last_upd: str = str(data.get("last_updated", ""))
        if last_upd:
            try:
                dt_upd: datetime = datetime.fromisoformat(last_upd)
                self.refresh_btn.title = f"Dernière mise à jour : {dt_upd.strftime('%H:%M')}"
            except (ValueError, TypeError):
                self.refresh_btn.title = "Dernière mise à jour : —"
        else:
            self.refresh_btn.title = "Dernière mise à jour : —"

    def _check_notifications(self, data: dict) -> None:
        """Émet une notification si nouveau mail ou changement d'événement.

        Invariant : on ne notifie un nouveau mail QUE si `gmail` dépasse
        `_prev_unread_total`. `clear_gmail_local` aligne ce seuil pour
        éviter de re-notifier des mails déjà « marqués lus » côté UI.
        """
        gmail: int = int(data.get("unread_gmail", 0))
        if gmail > self._prev_unread_total:
            diff: int = gmail - self._prev_unread_total
            send_notification(
                title="📧 Nouveaux emails",
                message=f"{diff} nouveau{'x' if diff > 1 else ''} email{'s' if diff > 1 else ''}",
                subtitle=f"Gmail : {gmail} non lu{'s' if gmail > 1 else ''}",
            )
        self._prev_unread_total = gmail

        event: Optional[dict] = _extract_event(data)
        if event is not None:
            title: str = str(event.get("title", ""))
            if title != self._prev_event_title and self._prev_event_title is not None:
                start: str = str(event.get("start", ""))
                end: str = str(event.get("end", ""))
                send_notification(
                    title="📅 Prochain événement",
                    message=title,
                    subtitle=format_event_time(start, end) if start else "",
                )
            self._prev_event_title = title
        else:
            self._prev_event_title = None


if __name__ == "__main__":
    _hide_dock_icon()
    DashboardMenubar().run()

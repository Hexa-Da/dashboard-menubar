#!/usr/bin/env python3
"""
Dashboard Menubar

Affiche dans la barre de menus macOS :
  - le prochain événement Google Calendar ;
  - Gmail : nombre de non lus + expéditeur / résumé du dernier ;
  - Zimbra (UL, IMAP) : idem, section séparée ;
  - badge sur la cloche : total Gmail + Zimbra (affichage UI) ;
  - actions : marquer Gmail/Zimbra comme lus (UI seulement),
    forcer une mise à jour, quitter.

Les données sont lues depuis dashboard.json à la racine du projet

Prérequis : `pip install rumps pyobjc-framework-Cocoa`.
"""

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Callable, Optional

import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSImageLeft
from Foundation import NSProcessInfo, NSActivityUserInitiatedAllowingIdleSystemSleep

from load_env import load_project_env

load_project_env()

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
DATA_FILE: str = os.path.join(_SCRIPT_DIR, "dashboard.json")
REFRESH_INTERVAL: int = 10  # secondes entre deux relectures du JSON
UPDATE_INTERVAL: int = 120  # secondes entre deux fetch gws (collecte des données)
UPDATE_SCRIPT: str = os.path.join(_SCRIPT_DIR, "dashboard_update.py")
UPDATE_LOG: str = os.path.join(_SCRIPT_DIR, "logs", "dashboard-update.log")
ICON_BELL: str = os.path.join(_SCRIPT_DIR, "assets", "bell.png")
ICON_MENUBAR_HEIGHT: float = 21.0  # hauteur cible ; la largeur suit le ratio du PNG

URL_GOOGLE_CALENDAR: str = "https://calendar.google.com/calendar/u/0/r"
URL_GMAIL: str = "https://mail.google.com/mail/u/0/#inbox"
URL_ZIMBRA: str = "https://mail.etu.univ-lorraine.fr/"

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


def _run_detached(cmd: list[str]) -> None:
    """Lance une commande en thread détaché, avec timeout.

    CRITIQUE : les notifs partent depuis le thread principal (timer rumps).
    Un `subprocess.run` synchrone qui bloque y gèlerait toute la run loop
    (et donc tout le rafraîchissement). On l'isole donc systématiquement.
    """
    def _go() -> None:
        try:
            subprocess.run(cmd, capture_output=True, timeout=15)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def _osa_escape(text: str) -> str:
    """Échappe une chaîne pour l'insérer dans un littéral AppleScript."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(title: str, message: str, subtitle: str = "") -> None:
    """Envoie une notification macOS via osascript, en thread détaché
    (jamais bloquant pour la run loop)."""
    script: str = (
        f'display notification "{_osa_escape(message)}" '
        f'with title "{_osa_escape(title)}" subtitle "{_osa_escape(subtitle)}"'
    )
    _run_detached(["osascript", "-e", script])


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
            sz = self._icon_nsimage.size()
            width: float = (
                ICON_MENUBAR_HEIGHT * sz.width / sz.height if sz.height else ICON_MENUBAR_HEIGHT
            )
            self._icon_nsimage.setSize_((width, ICON_MENUBAR_HEIGHT))

        # État interne
        self._prev_unread_total: int = 0
        self._prev_event_title: Optional[str] = None
        self._gmail_cleared: bool = False
        self._last_known_unread: int = 0
        # Zimbra : mêmes mécaniques que Gmail.
        self._prev_unread_zimbra: int = 0
        self._zimbra_cleared: bool = False
        self._last_known_unread_zimbra: int = 0
        self._button_configured: bool = False
        self._update_lock: threading.Lock = threading.Lock()

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
        self.mail_summary = rumps.MenuItem(
            "   💬 —", callback=_open_in_browser(URL_GMAIL)
        )

        # ── Zimbra ─────────────────────────────────────
        self.mail_zimbra = rumps.MenuItem(
            "✉️ Zimbra : —", callback=_open_in_browser(URL_ZIMBRA)
        )
        self.zimbra_from = rumps.MenuItem(
            "   👤 —", callback=_open_in_browser(URL_ZIMBRA)
        )
        self.zimbra_summary = rumps.MenuItem(
            "   💬 —", callback=_open_in_browser(URL_ZIMBRA)
        )

        # ── Actions ────────────────────────────────────
        self.last_updated_btn = rumps.MenuItem(
            "Dernière mise à jour : —",
            callback=_open_in_browser(f"file://{DATA_FILE}"),
        )
        self.mail_clear_btn = rumps.MenuItem(
            "Marquer Gmail comme lu", callback=self.clear_gmail_local
        )
        self.zimbra_clear_btn = rumps.MenuItem(
            "Marquer Zimbra comme lu", callback=self.clear_zimbra_local
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
            self.mail_summary,
            None,
            self.mail_zimbra,
            self.zimbra_from,
            self.zimbra_summary,
            None,
            self.last_updated_btn,
            self.mail_clear_btn,
            self.zimbra_clear_btn,
            self.force_update_btn,
            None,
            self.quit_btn,
        ]

        self._start_refresh_timer()
        self._start_update_timer()
        self._prevent_app_nap()
        rumps.events.on_wake.register(self._on_wake)
        self._run_update_once()  # premier fetch immédiat au démarrage

    # ─────────────────────────────────────────
    # Actions
    # ─────────────────────────────────────────

    def _start_refresh_timer(self) -> None:
        """(Re)démarre le timer rumps qui relit le JSON toutes les
        REFRESH_INTERVAL secondes sur le main run loop (thread-safe UI).

        Idempotent : arrête un timer existant avant d'en recréer un (le
        NSTimer peut cesser de tirer après une veille → cf. _on_wake)."""
        existing = getattr(self, "_refresh_timer", None)
        if existing is not None:
            try:
                existing.stop()
            except Exception:
                pass
        self._refresh_timer = rumps.Timer(self._on_refresh_tick, REFRESH_INTERVAL)
        self._refresh_timer.start()

    def _on_refresh_tick(self, _: object) -> None:
        """Callback du timer d'affichage — s'exécute sur le main thread."""
        if not self._button_configured:
            self._configure_status_button()
        try:
            self.refresh_data()
        except Exception:
            pass

    def _start_update_timer(self) -> None:
        """(Re)démarre le timer qui déclenche la collecte gws toutes les
        UPDATE_INTERVAL secondes. Le menubar pilote lui-même les updates
        (plus de LaunchAgent dashboard-update, trop fragile face aux veilles).

        Idempotent (cf. _on_wake)."""
        existing = getattr(self, "_update_timer", None)
        if existing is not None:
            try:
                existing.stop()
            except Exception:
                pass
        self._update_timer = rumps.Timer(self._on_update_tick, UPDATE_INTERVAL)
        self._update_timer.start()

    def _on_update_tick(self, _: object) -> None:
        """Tick du timer d'update (main thread) → lance un fetch en fond."""
        self._run_update_once()

    def _run_update_once(self) -> None:
        """Exécute dashboard_update.py en arrière-plan, un seul à la fois.

        Le fetch gws/OpenClaw est lent (jusqu'à ~2 min) : il ne doit JAMAIS
        tourner sur le thread principal. Le verrou évite les chevauchements
        (tick périodique + réveil)."""
        if not self._update_lock.acquire(blocking=False):
            return  # un fetch est déjà en cours

        def _go() -> None:
            try:
                os.makedirs(os.path.dirname(UPDATE_LOG), exist_ok=True)
                with open(UPDATE_LOG, "a", encoding="utf-8") as logf:
                    subprocess.run(
                        [sys.executable, UPDATE_SCRIPT],
                        stdout=logf, stderr=logf, timeout=150,
                    )
            except Exception:
                pass
            finally:
                self._update_lock.release()

        threading.Thread(target=_go, daemon=True).start()

    def _on_wake(self) -> None:
        """Réveil du Mac (event rumps `on_wake`).

        Après une veille, les NSTimer peuvent ne plus tirer → on les relance,
        on rafraîchit l'affichage et on déclenche un fetch immédiat pour ne
        pas rater de nouveaux mails. Indispensable car le Mac fait de
        fréquents « Maintenance Sleep »."""
        self._start_refresh_timer()
        self._start_update_timer()
        self._on_refresh_tick(None)
        self._run_update_once()

    def _prevent_app_nap(self) -> None:
        """Empêche App Nap de geler les timers de ce process accessoire
        quand il est inactif (le Mac peut toujours se mettre en veille)."""
        try:
            self._activity_token = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                NSActivityUserInitiatedAllowingIdleSystemSleep,
                "Dashboard menubar refresh timer",
            )
        except Exception:
            self._activity_token = None

    def _configure_status_button(self) -> None:
        """Force imagePosition=NSImageLeft pour que le titre (chiffre)
        n'élargisse le status item que vers la droite."""
        try:
            button = self._nsapp.nsstatusitem.button()
            button.setImagePosition_(NSImageLeft)
            button.setImageHugsTitle_(True)  # colle le chiffre contre le bell
            self._button_configured = True
        except Exception:
            pass

    def _refresh_badge(self) -> None:
        """Recalcule le badge cloche = Gmail affiché + Zimbra affiché."""
        gmail_shown: int = 0 if self._gmail_cleared else self._last_known_unread
        zimbra_shown: int = 0 if self._zimbra_cleared else self._last_known_unread_zimbra
        total: int = gmail_shown + zimbra_shown
        self.title = str(total) if total > 0 else ""

    def clear_gmail_local(self, _: object) -> None:
        """Marque les mails comme « lus » côté interface seulement.

        Effets : flag `_gmail_cleared` activé + seuil mémorisé. Tant qu'aucun
        nouveau mail (au-delà du seuil) n'arrive, l'UI affiche 0.
        """
        self._gmail_cleared = True
        self._prev_unread_total = self._last_known_unread
        self.mail_gmail.title = "✉️ Gmail : 0 non lu"
        self.mail_from.title = "   👤 —"
        self.mail_summary.title = "   💬 —"
        self._refresh_badge()

    def clear_zimbra_local(self, _: object) -> None:
        """Marque les mails Zimbra comme « lus » côté interface seulement.

        Symétrique de clear_gmail_local : flag + seuil mémorisé. IMAP reste
        en readonly, donc rien n'est modifié côté serveur.
        """
        self._zimbra_cleared = True
        self._prev_unread_zimbra = self._last_known_unread_zimbra
        self.mail_zimbra.title = "✉️ Zimbra : 0 non lu"
        self.zimbra_from.title = "   👤 —"
        self.zimbra_summary.title = "   💬 —"
        self._refresh_badge()

    def force_update(self, _: object) -> None:
        """Force une collecte immédiate.

        Délègue au même script que la collecte périodique (dashboard_update.py)
        pour une logique unique et robuste, sous le verrou partagé (pas de
        collecte concurrente). L'affichage se rafraîchit via le timer ; on
        notifie à la fin.
        """

        def _run() -> None:
            self.force_update_btn.title = "Mise à jour en cours..."
            ok: bool = True
            try:
                with self._update_lock:  # attend une collecte périodique en cours
                    os.makedirs(os.path.dirname(UPDATE_LOG), exist_ok=True)
                    with open(UPDATE_LOG, "a", encoding="utf-8") as logf:
                        subprocess.run(
                            [sys.executable, UPDATE_SCRIPT],
                            stdout=logf, stderr=logf, timeout=150,
                        )
            except Exception as exc:
                ok = False
                send_notification("⚠️ Erreur mise à jour", str(exc)[:150])
            finally:
                self.force_update_btn.title = "Forcer la mise à jour"
            if ok:
                send_notification(title="✅ Dashboard mis à jour", message="")

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
        gws_auth_error: bool = data.get("gws_auth_status") == "auth_error"
        event: Optional[dict] = _extract_event(data)
        if event is None:
            cal_title: str = "📅 Aucun événement à venir"
            if gws_auth_error:
                cal_title += " 🔑"
            self.event_title.title = cal_title
            if gws_auth_error:
                self.event_time.title = "   🔑 Token gws — gws auth login"
            else:
                self.event_time.title = "   ⏰ —"
            self.event_location.title = "   📍 —"
        else:
            title: str = str(event.get("title", "Événement sans titre"))
            cal_title = f"📅 {title}"
            if gws_auth_error:
                cal_title += " 🔑"
            self.event_title.title = cal_title

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
        # Un nouveau mail (compteur en hausse vs la lecture précédente) annule
        # le « marqué comme lu » manuel — même si le total était repassé bas
        # entre-temps (mails lus puis nouveau mail).
        if self._gmail_cleared and gmail_raw > self._last_known_unread:
            self._gmail_cleared = False
        self._last_known_unread = gmail_raw
        gmail_shown: int = 0 if self._gmail_cleared else gmail_raw

        gmail_title: str = (
            f"✉️ Gmail : {gmail_shown} non lu{'s' if gmail_shown > 1 else ''}"
        )
        # Auth gws (token révoqué) prioritaire sur l'avertissement réseau générique.
        if gws_auth_error:
            gmail_title += " 🔑"
        elif data.get("gmail_status") == "error":
            gmail_title += " ⚠️"
        self.mail_gmail.title = gmail_title

        latest: object = data.get("latest_unread")
        if isinstance(latest, dict) and gmail_shown > 0:
            sender: str = _format_mail_sender(str(latest.get("from", "")))
            summary: str = (
                str(latest.get("summary", "")).strip()
                or str(latest.get("subject", "")).strip()
                or "(sans objet)"
            )
            if len(sender) > 60:
                sender = sender[:57] + "…"
            if len(summary) > 70:
                summary = summary[:67] + "…"
            self.mail_from.title = f"   👤 {sender}"
            self.mail_summary.title = f"   💬 {summary}"
        else:
            self.mail_from.title = "   👤 —"
            self.mail_summary.title = "   💬 —"

        # ── Zimbra ────────────────────────────────────
        zimbra_raw: int = int(data.get("unread_zimbra", 0))
        if self._zimbra_cleared and zimbra_raw > self._last_known_unread_zimbra:
            self._zimbra_cleared = False
        self._last_known_unread_zimbra = zimbra_raw
        zimbra_shown: int = 0 if self._zimbra_cleared else zimbra_raw

        zimbra_title: str = (
            f"✉️ Zimbra : {zimbra_shown} non lu{'s' if zimbra_shown > 1 else ''}"
        )
        if data.get("zimbra_status") == "error":
            zimbra_title += " ⚠️"
        self.mail_zimbra.title = zimbra_title

        latest_z: object = data.get("latest_unread_zimbra")
        if isinstance(latest_z, dict) and zimbra_shown > 0:
            z_sender: str = _format_mail_sender(str(latest_z.get("from", "")))
            z_summary: str = (
                str(latest_z.get("summary", "")).strip()
                or str(latest_z.get("subject", "")).strip()
                or "(sans objet)"
            )
            if len(z_sender) > 60:
                z_sender = z_sender[:57] + "…"
            if len(z_summary) > 70:
                z_summary = z_summary[:67] + "…"
            self.zimbra_from.title = f"   👤 {z_sender}"
            self.zimbra_summary.title = f"   💬 {z_summary}"
        else:
            self.zimbra_from.title = "   👤 —"
            self.zimbra_summary.title = "   💬 —"

        # ── Badge cloche : Gmail + Zimbra ─────────────
        total_shown: int = gmail_shown + zimbra_shown
        self.title = str(total_shown) if total_shown > 0 else ""
        last_upd: str = str(data.get("last_updated", ""))
        if last_upd:
            try:
                dt_upd: datetime = datetime.fromisoformat(last_upd)
                self.last_updated_btn.title = f"Dernière mise à jour : {dt_upd.strftime('%H:%M')}"
            except (ValueError, TypeError):
                self.last_updated_btn.title = "Dernière mise à jour : —"
        else:
            self.last_updated_btn.title = "Dernière mise à jour : —"

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

        zimbra: int = int(data.get("unread_zimbra", 0))
        if zimbra > self._prev_unread_zimbra:
            diff_z: int = zimbra - self._prev_unread_zimbra
            send_notification(
                title="📧 Nouveaux mails Zimbra",
                message=f"{diff_z} nouveau{'x' if diff_z > 1 else ''} mail{'s' if diff_z > 1 else ''}",
                subtitle=f"Zimbra : {zimbra} non lu{'s' if zimbra > 1 else ''}",
            )
        self._prev_unread_zimbra = zimbra

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

#!/usr/bin/env python3
"""
Dashboard Menubar

Affiche dans la barre de menus macOS :
  - le prochain événement Google Calendar ;
  - Gmail : nombre de non lus + expéditeur / résumé du dernier ;
  - Zimbra (UL, IMAP) : idem, section séparée ;
  - badge sur la cloche : total Gmail + Zimbra (affichage UI) ;
  - actions : marquer lu / corbeille du mail Gmail/Zimbra affiché (vedette),
    forcer une mise à jour, quitter.

Les données sont lues depuis dashboard.json à la racine du projet

Prérequis : `pip install rumps pyobjc-framework-Cocoa`.
"""

import contextlib
import json
import os
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Callable, Optional, TextIO

import rumps
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSImageLeft,
    NSRunningApplication,
    NSWorkspace,
)
from Foundation import (
    NSActivityUserInitiatedAllowingIdleSystemSleep,
    NSBundle,
    NSOperationQueue,
    NSProcessInfo,
    NSURL,
)

import dashboard_update
import mac_notify
from load_env import load_project_env
from mail_trash import (
    mark_gmail_message_read,
    mark_zimbra_message_read,
    trash_gmail_message,
    trash_zimbra_message,
)
from zimbra_unread import DEFAULT_HOST, DEFAULT_PORT

load_project_env()

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────

_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
DATA_FILE: str = os.path.join(_SCRIPT_DIR, "dashboard.json")
REFRESH_INTERVAL: int = 10  # secondes entre deux relectures du JSON
UPDATE_INTERVAL: int = 60  # secondes entre deux fetch gws (collecte des données)
# Rappel notif auth si gws_auth_status reste en erreur (amorce / redémarrage).
# Horloge monotonic dans _check_notifications (pas de NSTimer dédié).
AUTH_REMIND_INTERVAL: int = 30 * 60
AUTH_NOTIF_PREFIX: str = "gws-auth-"
# Rappel notif si la collecte périodique échoue (in-process).
UPDATE_FAIL_REMIND_INTERVAL: int = 30 * 60
UPDATE_FAIL_NOTIF_PREFIX: str = "update-fail-"
# Nb max de ticks de refresh pendant lesquels on diffère une notif mail en
# attendant le résumé IA. Garde-fou : au-delà, on notifie quand même
# (si Azure OpenAI échoue, ne jamais notifier serait pire). ~2 min à 10 s/tick.
MAX_NOTIF_DEFER_TICKS: int = 12
UPDATE_LOG: str = os.path.join(_SCRIPT_DIR, "logs", "dashboard-update.log")
UPDATE_LOG_MAX_BYTES: int = 512 * 1024
UPDATE_LOG_BACKUP_COUNT: int = 3
# Watchdog indépendant des NSTimer (peuvent cesser après veille / App Nap).
WATCHDOG_INTERVAL: int = 30  # secondes entre deux contrôles
STALE_DATA_SECONDS: int = UPDATE_INTERVAL + 60  # JSON trop vieux → fetch
STALE_TICK_SECONDS: int = 45  # refresh timer (10 s) n'a pas tiré → recovery
STALE_RESTART_SECONDS: int = 180  # recovery inefficace → redémarrage KeepAlive
UPDATE_LOCK_TIMEOUT: int = 160  # secondes (bornes gws/IMAP/Azure + marge)
# Warmup post-veille : probe TCP sortant avant première collecte / notifs auth.
WAKE_SETUP_DEADLINE: float = 40.0
WAKE_PROBE_TIMEOUT: float = 1.5
WAKE_PROBE_INTERVAL: float = 2.0
WAKE_PROBE_HOST: str = "1.1.1.1"
WAKE_PROBE_PORT: int = 443
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


def _network_outbound_ok(
    host: str = WAKE_PROBE_HOST,
    port: int = WAKE_PROBE_PORT,
    timeout: float = WAKE_PROBE_TIMEOUT,
) -> bool:
    """True si une connexion TCP sortante vers host:port réussit."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _open_update_log() -> TextIO:
    """Ouvre le journal de collecte après rotation si nécessaire."""
    os.makedirs(os.path.dirname(UPDATE_LOG), exist_ok=True)
    try:
        should_rotate: bool = os.path.getsize(UPDATE_LOG) >= UPDATE_LOG_MAX_BYTES
    except OSError:
        should_rotate = False

    if should_rotate:
        oldest: str = f"{UPDATE_LOG}.{UPDATE_LOG_BACKUP_COUNT}"
        try:
            os.remove(oldest)
        except FileNotFoundError:
            pass
        for index in range(UPDATE_LOG_BACKUP_COUNT - 1, 0, -1):
            source: str = f"{UPDATE_LOG}.{index}"
            destination: str = f"{UPDATE_LOG}.{index + 1}"
            try:
                os.replace(source, destination)
            except FileNotFoundError:
                pass
        os.replace(UPDATE_LOG, f"{UPDATE_LOG}.1")

    return open(UPDATE_LOG, "a", encoding="utf-8")


def _run_update_script(logf: TextIO) -> tuple[str, str]:
    """Exécute `dashboard_update.main()` in-process ; retourne (`ok`|`error`, message).

    Précondition : `logf` ouvert en append (journal de collecte).
    Invariant : succès sans exception → (`ok`, "") ; sinon (`error`, détail tronqué).
    Pas de second interpréteur venv (évite PermissionError TCC sous Documents).
    """
    try:
        with contextlib.redirect_stdout(logf), contextlib.redirect_stderr(logf):
            dashboard_update.main()
    except Exception as exc:
        return "error", str(exc)[:150]
    return "ok", ""


def _open_url(url: str) -> None:
    """Ouvre `url` dans le navigateur par défaut (chemin Firefox cold-start inclus)."""
    target_url = NSURL.URLWithString_(url)
    browser_url = NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(
        target_url
    )
    browser_bundle = NSBundle.bundleWithURL_(browser_url) if browser_url else None
    bundle_id = browser_bundle.bundleIdentifier() if browser_bundle else None

    if bundle_id == "org.mozilla.firefox" and not (
        NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
    ):
        # Cold start via LaunchServices + URL en arg : profil OK, pas d'onglet vide.
        # (Popen du binaire Mach-O cassait le chargement du profil Firefox.)
        subprocess.run(
            ["open", "-b", bundle_id, "--args", url],
            check=False,
        )
        return

    subprocess.run(["open", url], check=False)


def _open_in_browser(url: str) -> Callable[[object], None]:
    """Retourne un callback rumps qui ouvre `url` dans le navigateur par défaut."""

    def _handler(_: object) -> None:
        _open_url(url)

    return _handler


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


def _mail_notification_body(mail: dict) -> str:
    """Corps de la notif = résumé IA uniquement (jamais le sujet).

    Précondition : `mail` est un dict non vide.
    Invariant : tant que le résumé n'est pas prêt, on affiche un extrait ou
    un libellé d'attente — le sujet n'est jamais utilisé comme corps.
    """
    summary: str = str(mail.get("summary", "")).strip()
    if summary:
        if len(summary) > 120:
            return summary[:117] + "…"
        return summary
    snippet: str = str(mail.get("snippet", "")).strip()
    if snippet:
        if len(snippet) > 120:
            return snippet[:117] + "…"
        return snippet
    return "Résumé en cours…"


def _mail_notification_content(
    source_label: str,
    mail: Optional[dict],
) -> tuple[str, str, str]:
    """Construit title / subtitle / message pour une notif mail native.

    Format affiché dans le Centre de notifications :
      - title    : Gmail | Zimbra
      - subtitle : « Auteur : résumé » (une seule ligne)

    Précondition : `source_label` non vide.
    """
    title: str = source_label
    if not isinstance(mail, dict):
        return title, "— : Nouveau message non lu", ""
    author: str = _format_mail_sender(str(mail.get("from", "")))
    summary: str = _mail_notification_body(mail)
    if len(author) > 60:
        author = author[:57] + "…"
    line: str = f"{author} : {summary}"
    if len(line) > 180:
        line = line[:177] + "…"
    return title, line, ""


def _extract_event(data: dict) -> Optional[dict]:
    """Renvoie le prochain événement pertinent (ou None).

    Ignore un événement dont la fin est dans moins de 15 minutes.
    Accepte `next_events` (liste) et `next_event` (objet legacy).
    """
    now: datetime = datetime.now()
    cutoff: timedelta = timedelta(minutes=15)

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
        self._prev_event_key: Optional[tuple[str, str, str, str]] = None
        self._prev_gws_auth_status: Optional[str] = None
        # Ancre monotonic du dernier rappel auth (amorce ou deliver) ;
        # None = pas encore amorcé. Immune aux reset NSTimer wake/recover.
        self._auth_remind_anchor: Optional[float] = None
        # Collecte périodique en échec : sticky + rappel (cf. auth gws).
        self._update_collect_failed: bool = False
        self._update_fail_remind_anchor: Optional[float] = None
        self._update_last_fail_msg: str = ""

        # Notifications natives par source (cf. mac_notify) :
        #  Une seule notification par source (gmail-current / zimbra-current) pour
        #  éviter l'empilement.  _seen_* garde les ids déjà vus (anti-re-notif) ;
        #  _active_*_id est le mail du slot ("" = vacant → promouvoir le latest) ;
        #  _*_banner_live : True seulement après un deliver réel (amorce sans
        #  bannière ne doit pas déclencher de MAJ corps → deliver au 1er résumé).
        self._notif_initialized: bool = False
        self._seen_gmail_ids: set[str] = set()
        self._active_gmail_id: str = ""
        self._active_gmail_body: str = ""
        self._gmail_banner_live: bool = False
        self._notif_defer_gmail: int = 0
        self._seen_zimbra_ids: set[str] = set()
        self._active_zimbra_id: str = ""
        self._active_zimbra_body: str = ""
        self._zimbra_banner_live: bool = False
        self._notif_defer_zimbra: int = 0
        self._button_configured: bool = False
        # Clic / Afficher sur notif mail|événement → page web générique.
        mac_notify.set_activation_handler(self._on_notification_activate)
        self._update_lock: threading.Lock = threading.Lock()
        self._last_refresh_tick_at: float = time.monotonic()
        # Post-veille : False jusqu'au probe réseau OK (ou deadline).
        self._wake_ready: bool = True
        self._wake_setup_gen: int = 0
        self._trash_gmail_busy: bool = False
        self._trash_zimbra_busy: bool = False
        self._mark_gmail_busy: bool = False
        self._mark_zimbra_busy: bool = False

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
        self.mail_mark_gmail_btn = rumps.MenuItem(
            "Marquer last_unread Gmail comme lu",
            callback=self.mark_gmail_featured,
        )
        self.mail_mark_zimbra_btn = rumps.MenuItem(
            "Marquer last_unread Zimbra comme lu",
            callback=self.mark_zimbra_featured,
        )
        self.mail_trash_gmail_btn = rumps.MenuItem(
            "Supprimer last_unread Gmail", callback=self.trash_gmail_featured
        )
        self.mail_trash_zimbra_btn = rumps.MenuItem(
            "Supprimer last_unread Zimbra", callback=self.trash_zimbra_featured
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
            self.mail_mark_gmail_btn,
            self.mail_mark_zimbra_btn,
            self.mail_trash_gmail_btn,
            self.mail_trash_zimbra_btn,
            self.force_update_btn,
            None,
            self.quit_btn,
        ]

        self._start_refresh_timer()
        self._start_update_timer()
        self._start_watchdog()
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
        self._last_refresh_tick_at = time.monotonic()
        if not self._button_configured:
            self._configure_status_button()
        try:
            self.refresh_data()
        except Exception:
            pass

    def _run_on_main(self, fn: Callable[[], None]) -> None:
        """Planifie `fn` sur le main thread (AppKit / rumps)."""
        NSOperationQueue.mainQueue().addOperationWithBlock_(fn)

    def _dashboard_json_age_seconds(self) -> Optional[float]:
        """Âge du fichier JSON en secondes, ou None si absent/inaccessible."""
        try:
            return time.time() - os.path.getmtime(DATA_FILE)
        except OSError:
            return None

    def _recover_timers(self) -> None:
        """Relance les NSTimer et rafraîchit l'UI (main thread uniquement)."""
        self._start_refresh_timer()
        self._start_update_timer()
        try:
            self.refresh_data()
        except Exception:
            pass

    def _watchdog_tick(self) -> None:
        """Contrôle santé timers + données (thread watchdog, pas le main thread).

        Préconditions : app démarrée, watchdog actif.
        Invariants :
          - si le JSON est trop vieux, on lance un fetch (même si NSTimer mort) ;
          - si le refresh timer ne tire plus, on tente recovery sur le main thread ;
          - si la run loop reste gelée, SIGTERM + KeepAlive redémarre le process.
        """
        now: float = time.monotonic()
        tick_age: float = now - self._last_refresh_tick_at

        json_age: Optional[float] = self._dashboard_json_age_seconds()
        if json_age is not None and json_age > STALE_DATA_SECONDS:
            self._run_update_once()

        if tick_age <= STALE_TICK_SECONDS:
            return

        self._run_on_main(self._recover_timers)

        if tick_age > STALE_RESTART_SECONDS:
            os.kill(os.getpid(), signal.SIGTERM)

    def _start_watchdog(self) -> None:
        """Thread watchdog indépendant de la run loop AppKit."""

        def _loop() -> None:
            while True:
                time.sleep(WATCHDOG_INTERVAL)
                try:
                    self._watchdog_tick()
                except Exception:
                    pass

        threading.Thread(target=_loop, daemon=True, name="dashboard-watchdog").start()

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

    def _on_notification_activate(self, identifier: str) -> None:
        """Clic / Afficher sur une notif → page web générique (même URLs que le menu).

        Ids hors mapping (auth, collecte, update-status) : no-op volontaire.
        """
        url_by_id: dict[str, str] = {
            "gmail-current": URL_GMAIL,
            "zimbra-current": URL_ZIMBRA,
            "event-current": URL_GOOGLE_CALENDAR,
        }
        url: Optional[str] = url_by_id.get(identifier)
        if url:
            _open_url(url)

    def _deliver_gws_auth_notif(self) -> None:
        """Notif sticky « token Google expiré » (transition ou rappel 30 min).

        Id horodaté + son : même identifier + même texte ne re-affiche
        souvent pas de bannière sous NSUserNotification (notifs livrées
        silencieusement au Centre uniquement).
        No-op pendant warmup réveil (alertes setup silencieuses).
        """
        if not self._wake_ready:
            return
        mac_notify.remove_prefix(AUTH_NOTIF_PREFIX)
        stamp: str = time.strftime("%H:%M")
        mac_notify.deliver(
            f"{AUTH_NOTIF_PREFIX}{int(time.time())}",
            "🔑 Token Google expiré",
            "Exécuter : gws auth login",
            f"Dashboard menubar · {stamp}",
            sound=True,
        )
        self._auth_remind_anchor = time.monotonic()

    def _deliver_update_fail_notif(self, message: str) -> None:
        """Notif sticky « collecte en échec » (1er échec ou rappel 30 min).

        Précondition : main thread. Id horodaté + son (même motif que auth).
        No-op pendant warmup réveil (alertes setup silencieuses).
        """
        if not self._wake_ready:
            return
        detail: str = (message or "Collecte impossible").strip()[:150]
        mac_notify.remove_prefix(UPDATE_FAIL_NOTIF_PREFIX)
        stamp: str = time.strftime("%H:%M")
        mac_notify.deliver(
            f"{UPDATE_FAIL_NOTIF_PREFIX}{int(time.time())}",
            "⚠️ Collecte dashboard en échec",
            detail,
            f"Dashboard menubar · {stamp}",
            sound=True,
        )
        self._update_collect_failed = True
        self._update_last_fail_msg = detail
        self._update_fail_remind_anchor = time.monotonic()

    def _clear_update_fail_notif(self) -> None:
        """Retire les notifs collecte en échec (main thread)."""
        mac_notify.remove_prefix(UPDATE_FAIL_NOTIF_PREFIX)
        self._update_collect_failed = False
        self._update_fail_remind_anchor = None
        self._update_last_fail_msg = ""

    def _note_update_collect_result(self, status: str, err_msg: str = "") -> None:
        """Applique le résultat d'une collecte sur l'état sticky (main thread).

        Premier échec → bannière ; échecs suivants → rappel via refresh.
        Succès → clear. Pendant warmup réveil : mémoriser sans bannière.
        """
        if status == "ok":
            self._clear_update_fail_notif()
            return
        first_failure: bool = not self._update_collect_failed
        self._update_collect_failed = True
        self._update_last_fail_msg = (err_msg or "Collecte impossible").strip()[:150]
        if first_failure and self._wake_ready:
            self._deliver_update_fail_notif(self._update_last_fail_msg)

    def _run_update_once(self) -> None:
        """Exécute la collecte in-process en arrière-plan, un seul à la fois.

        Le fetch gws/Azure est lent (jusqu'à ~2 min) : il ne doit JAMAIS
        tourner sur le thread principal. Le verrou évite les chevauchements
        (tick périodique + réveil). Échec (exception) → notif sticky via
        `_note_update_collect_result` sur le main thread.
        No-op pendant le warmup post-veille (`_wake_ready` False).
        """
        if not self._wake_ready:
            return
        if not self._update_lock.acquire(blocking=False):
            return  # un fetch est déjà en cours

        def _go() -> None:
            status: str = "error"
            err_msg: str = ""
            try:
                with _open_update_log() as logf:
                    status, err_msg = _run_update_script(logf)
            except Exception as exc:
                status = "error"
                err_msg = str(exc)[:150]
            finally:
                self._update_lock.release()
            self._run_on_main(
                lambda: self._note_update_collect_result(status, err_msg)
            )

        threading.Thread(target=_go, daemon=True).start()

    def _on_wake(self) -> None:
        """Réveil du Mac (event rumps `on_wake`).

        Après une veille, les NSTimer peuvent ne plus tirer → on les relance
        et on rafraîchit l'UI. La collecte attend le warmup réseau
        (`_wake_ready`) ; alertes setup (auth, collecte, offline/stale UI)
        restent silencieuses jusqu'à la fin du setup.
        """
        self._wake_ready = False
        self._wake_setup_gen += 1
        gen: int = self._wake_setup_gen
        # Retirer les bannières setup encore visibles (auth / collecte).
        mac_notify.remove_prefix(AUTH_NOTIF_PREFIX)
        mac_notify.remove_prefix(UPDATE_FAIL_NOTIF_PREFIX)
        self._start_refresh_timer()
        self._start_update_timer()
        self._on_refresh_tick(None)
        threading.Thread(
            target=self._wake_setup_worker,
            args=(gen,),
            daemon=True,
            name="wake-setup",
        ).start()

    def _wake_setup_worker(self, gen: int) -> None:
        """Probe TCP jusqu'à réseau OK ou deadline, puis finish sur main."""
        deadline: float = time.monotonic() + WAKE_SETUP_DEADLINE
        while time.monotonic() < deadline:
            if gen != self._wake_setup_gen:
                return
            if _network_outbound_ok():
                break
            time.sleep(WAKE_PROBE_INTERVAL)
        if gen != self._wake_setup_gen:
            return
        self._run_on_main(lambda: self._finish_wake_setup(gen))

    def _finish_wake_setup(self, gen: int) -> None:
        """Ouvre le gate post-veille et lance la première collecte (main thread)."""
        if gen != self._wake_setup_gen:
            return
        self._wake_ready = True
        # Ré-amorce alertes setup : pas de burst immédiat post-warmup.
        data: dict = load_data()
        auth_status: str = str(data.get("gws_auth_status", "ok"))
        self._prev_gws_auth_status = auth_status
        if auth_status == "auth_error":
            self._auth_remind_anchor = time.monotonic()
        else:
            self._auth_remind_anchor = None
            mac_notify.remove_prefix(AUTH_NOTIF_PREFIX)
        if self._update_collect_failed:
            self._update_fail_remind_anchor = time.monotonic()
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

    def mark_gmail_featured(self, _: object) -> None:
        """Marque comme lu le mail Gmail affiché (latest_unread.id)."""
        if self._mark_gmail_busy:
            return
        self._mark_gmail_busy = True
        self.mail_mark_gmail_btn.title = "Marquer last_unread Gmail comme lu…"
        self.mail_mark_gmail_btn.set_callback(None)
        threading.Thread(target=self._mark_gmail_worker, daemon=True).start()

    def mark_zimbra_featured(self, _: object) -> None:
        """Marque comme lu le mail Zimbra affiché (latest_unread_zimbra.id)."""
        if self._mark_zimbra_busy:
            return
        self._mark_zimbra_busy = True
        self.mail_mark_zimbra_btn.title = "Marquer last_unread Zimbra comme lu…"
        self.mail_mark_zimbra_btn.set_callback(None)
        threading.Thread(target=self._mark_zimbra_worker, daemon=True).start()

    def trash_gmail_featured(self, _: object) -> None:
        """Met à la corbeille le mail Gmail affiché (latest_unread.id)."""
        if self._trash_gmail_busy:
            return
        self._trash_gmail_busy = True
        self.mail_trash_gmail_btn.title = "Supprimer last_unread Gmail…"
        self.mail_trash_gmail_btn.set_callback(None)
        threading.Thread(target=self._trash_gmail_worker, daemon=True).start()

    def trash_zimbra_featured(self, _: object) -> None:
        """Met à la corbeille le mail Zimbra affiché (latest_unread_zimbra.id)."""
        if self._trash_zimbra_busy:
            return
        self._trash_zimbra_busy = True
        self.mail_trash_zimbra_btn.title = "Supprimer last_unread Zimbra…"
        self.mail_trash_zimbra_btn.set_callback(None)
        threading.Thread(target=self._trash_zimbra_worker, daemon=True).start()

    def _mark_gmail_worker(self) -> None:
        """Mark-as-read Gmail + collecte (thread de fond)."""
        data: dict = load_data()
        latest: object = data.get("latest_unread")
        message_id: str = (
            str(latest.get("id", "")).strip() if isinstance(latest, dict) else ""
        )
        err: str = ""
        if not message_id:
            err = "Aucun mail Gmail affiché."
        else:
            try:
                mark_gmail_message_read(message_id)
            except Exception as exc:
                err = str(exc)[:150]

        if err:
            def _fail() -> None:
                self._restore_mark_gmail_btn()
                mac_notify.deliver("update-status", "⚠️ Lu Gmail", err)

            self._run_on_main(_fail)
            return

        def _marked() -> None:
            # Retrait visuel immédiat ; garder _active_* pour que _sync
            # détecte la sortie du mail et promeuve le suivant (évite
            # de re-notifier le même mail tant que le JSON n'est pas à jour).
            mac_notify.remove("gmail-current")
            self._gmail_banner_live = False

        self._run_on_main(_marked)
        self._collect_after_action(restore_btn="mark_gmail")

    def _mark_zimbra_worker(self) -> None:
        """Mark-as-read Zimbra + collecte (thread de fond)."""
        data: dict = load_data()
        latest: object = data.get("latest_unread_zimbra")
        uid: str = (
            str(latest.get("id", "")).strip() if isinstance(latest, dict) else ""
        )
        user: str = (os.environ.get("ZIMBRA_USER") or "").strip()
        password: str = (os.environ.get("ZIMBRA_PASS") or "").strip()
        err: str = ""
        if not uid:
            err = "Aucun mail Zimbra affiché."
        elif not user or not password:
            err = "ZIMBRA_USER / ZIMBRA_PASS manquants."
        else:
            try:
                host: str = os.environ.get("ZIMBRA_IMAP_HOST", DEFAULT_HOST)
                port: int = int(os.environ.get("ZIMBRA_IMAP_PORT", str(DEFAULT_PORT)))
                mark_zimbra_message_read(uid, user, password, host=host, port=port)
            except Exception as exc:
                err = str(exc)[:150]

        if err:
            def _fail() -> None:
                self._restore_mark_zimbra_btn()
                mac_notify.deliver("update-status", "⚠️ Lu Zimbra", err)

            self._run_on_main(_fail)
            return

        def _marked() -> None:
            mac_notify.remove("zimbra-current")
            self._zimbra_banner_live = False

        self._run_on_main(_marked)
        self._collect_after_action(restore_btn="mark_zimbra")

    def _trash_gmail_worker(self) -> None:
        """Trash Gmail + collecte (thread de fond)."""
        data: dict = load_data()
        latest: object = data.get("latest_unread")
        message_id: str = (
            str(latest.get("id", "")).strip() if isinstance(latest, dict) else ""
        )
        err: str = ""
        if not message_id:
            err = "Aucun mail Gmail affiché."
        else:
            try:
                trash_gmail_message(message_id)
            except Exception as exc:
                err = str(exc)[:150]

        if err:
            def _fail() -> None:
                self._restore_trash_gmail_btn()
                mac_notify.deliver("update-status", "⚠️ Corbeille Gmail", err)

            self._run_on_main(_fail)
            return

        def _trashed() -> None:
            mac_notify.remove("gmail-current")
            self._gmail_banner_live = False

        self._run_on_main(_trashed)
        self._collect_after_action(restore_btn="gmail")

    def _trash_zimbra_worker(self) -> None:
        """Trash Zimbra + collecte (thread de fond)."""
        data: dict = load_data()
        latest: object = data.get("latest_unread_zimbra")
        uid: str = (
            str(latest.get("id", "")).strip() if isinstance(latest, dict) else ""
        )
        user: str = (os.environ.get("ZIMBRA_USER") or "").strip()
        password: str = (os.environ.get("ZIMBRA_PASS") or "").strip()
        err: str = ""
        if not uid:
            err = "Aucun mail Zimbra affiché."
        elif not user or not password:
            err = "ZIMBRA_USER / ZIMBRA_PASS manquants."
        else:
            try:
                host: str = os.environ.get("ZIMBRA_IMAP_HOST", DEFAULT_HOST)
                port: int = int(os.environ.get("ZIMBRA_IMAP_PORT", str(DEFAULT_PORT)))
                trash_zimbra_message(uid, user, password, host=host, port=port)
            except Exception as exc:
                err = str(exc)[:150]

        if err:
            def _fail() -> None:
                self._restore_trash_zimbra_btn()
                mac_notify.deliver("update-status", "⚠️ Corbeille Zimbra", err)

            self._run_on_main(_fail)
            return

        def _trashed() -> None:
            mac_notify.remove("zimbra-current")
            self._zimbra_banner_live = False

        self._run_on_main(_trashed)
        self._collect_after_action(restore_btn="zimbra")

    def _restore_mark_gmail_btn(self) -> None:
        self._mark_gmail_busy = False
        self.mail_mark_gmail_btn.title = "Marquer last_unread Gmail comme lu"
        self.mail_mark_gmail_btn.set_callback(self.mark_gmail_featured)

    def _restore_mark_zimbra_btn(self) -> None:
        self._mark_zimbra_busy = False
        self.mail_mark_zimbra_btn.title = "Marquer last_unread Zimbra comme lu"
        self.mail_mark_zimbra_btn.set_callback(self.mark_zimbra_featured)

    def _restore_trash_gmail_btn(self) -> None:
        self._trash_gmail_busy = False
        self.mail_trash_gmail_btn.title = "Supprimer last_unread Gmail"
        self.mail_trash_gmail_btn.set_callback(self.trash_gmail_featured)

    def _restore_trash_zimbra_btn(self) -> None:
        self._trash_zimbra_busy = False
        self.mail_trash_zimbra_btn.title = "Supprimer last_unread Zimbra"
        self.mail_trash_zimbra_btn.set_callback(self.trash_zimbra_featured)

    def _collect_after_action(self, *, restore_btn: str) -> None:
        """Collecte sous verrou (déjà sur un worker), puis UI + notif sur main.

        `restore_btn` : \"gmail\" | \"zimbra\" | \"mark_gmail\" | \"mark_zimbra\" | \"force\".
        Succès : notif uniquement pour \"force\" ; mark/trash = refresh silencieux.
        Erreurs : toujours notifiées.
        """
        status: str = "ok"
        err_msg: str = ""
        try:
            acquired: bool = self._update_lock.acquire(timeout=UPDATE_LOCK_TIMEOUT)
            if not acquired:
                status = "blocked"
            else:
                try:
                    with _open_update_log() as logf:
                        status, err_msg = _run_update_script(logf)
                finally:
                    self._update_lock.release()
        except Exception as exc:
            status = "error"
            err_msg = str(exc)[:150]

        def _finish() -> None:
            if restore_btn == "gmail":
                self._restore_trash_gmail_btn()
            elif restore_btn == "zimbra":
                self._restore_trash_zimbra_btn()
            elif restore_btn == "mark_gmail":
                self._restore_mark_gmail_btn()
            elif restore_btn == "mark_zimbra":
                self._restore_mark_zimbra_btn()
            else:
                self.force_update_btn.title = "Forcer la mise à jour"
            if status == "blocked":
                mac_notify.deliver(
                    "update-status",
                    "⚠️ Mise à jour bloquée",
                    "Une collecte précédente ne répond pas (>2 min).",
                )
            elif status == "error":
                mac_notify.deliver(
                    "update-status",
                    "⚠️ Erreur mise à jour",
                    err_msg,
                )
                self._note_update_collect_result("error", err_msg)
            else:
                self._note_update_collect_result("ok")
                if restore_btn == "force":
                    mac_notify.deliver(
                        "update-status",
                        "✅ Dashboard mis à jour",
                        "",
                    )
                self.refresh_data()

        self._run_on_main(_finish)

    def force_update(self, _: object) -> None:
        """Force une collecte immédiate.

        Délègue à la même collecte in-process que le tick périodique,
        sous le verrou partagé (pas de collecte concurrente). Rafraîchit
        l'UI à la fin.
        Annule un warmup post-veille en cours (utilisateur force l'ouverture
        du gate).
        """
        self._wake_ready = True
        self._wake_setup_gen += 1
        self.force_update_btn.title = "Mise à jour en cours..."
        threading.Thread(target=self._force_update_worker, daemon=True).start()

    def _force_update_worker(self) -> None:
        """Exécute la collecte forcée (thread de fond, pas le main thread)."""
        self._collect_after_action(restore_btn="force")

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
        # Priorité d'affichage : offline > auth_error > error source.
        # Warmup réveil : garder les compteurs, masquer ✈️/🔑/⚠️ setup.
        show_setup_alerts: bool = self._wake_ready
        offline: bool = show_setup_alerts and data.get("connectivity") == "offline"
        gws_auth_error: bool = (
            show_setup_alerts
            and (not offline)
            and data.get("gws_auth_status") == "auth_error"
        )
        gmail_source_error: bool = (
            show_setup_alerts and data.get("gmail_status") == "error"
        )
        zimbra_source_error: bool = (
            show_setup_alerts and data.get("zimbra_status") == "error"
        )
        event: Optional[dict] = _extract_event(data)
        if event is None:
            cal_title: str = "📅 Aucun événement à venir"
            if offline:
                cal_title += " ✈️"
            elif gws_auth_error:
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
            if offline:
                cal_title += " ✈️"
            elif gws_auth_error:
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
        gmail_shown: int = int(data.get("unread_gmail", 0))

        gmail_title: str = (
            f"✉️ Gmail : {gmail_shown} non lu{'s' if gmail_shown > 1 else ''}"
        )
        if offline:
            gmail_title += " ✈️"
        elif gws_auth_error:
            gmail_title += " 🔑"
        elif gmail_source_error:
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
        zimbra_shown: int = int(data.get("unread_zimbra", 0))

        zimbra_title: str = (
            f"✉️ Zimbra : {zimbra_shown} non lu{'s' if zimbra_shown > 1 else ''}"
        )
        if offline:
            zimbra_title += " ✈️"
        elif zimbra_source_error:
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
        json_age: Optional[float] = self._dashboard_json_age_seconds()
        stale_or_fail: bool = show_setup_alerts and (
            self._update_collect_failed
            or (json_age is not None and json_age > STALE_DATA_SECONDS)
        )
        if last_upd:
            try:
                dt_upd: datetime = datetime.fromisoformat(last_upd)
                upd_title: str = f"Dernière mise à jour : {dt_upd.strftime('%H:%M')}"
            except (ValueError, TypeError):
                upd_title = "Dernière mise à jour : —"
        else:
            upd_title = "Dernière mise à jour : —"
        if stale_or_fail:
            upd_title += " ⚠️"
        self.last_updated_btn.title = upd_title

        # Activer Marquer lu / Corbeille seulement s'il y a une vedette avec id
        # (ne pas toucher aux boutons pendant une action en cours).
        latest_g: object = data.get("latest_unread")
        can_act_g: bool = (
            gmail_shown > 0
            and isinstance(latest_g, dict)
            and bool(str(latest_g.get("id", "")).strip())
        )
        if not self._mark_gmail_busy:
            if can_act_g:
                self.mail_mark_gmail_btn.set_callback(self.mark_gmail_featured)
            else:
                self.mail_mark_gmail_btn.set_callback(None)
        if not self._trash_gmail_busy:
            if can_act_g:
                self.mail_trash_gmail_btn.set_callback(self.trash_gmail_featured)
            else:
                self.mail_trash_gmail_btn.set_callback(None)

        latest_z_btn: object = data.get("latest_unread_zimbra")
        can_act_z: bool = (
            zimbra_shown > 0
            and isinstance(latest_z_btn, dict)
            and bool(str(latest_z_btn.get("id", "")).strip())
        )
        if not self._mark_zimbra_busy:
            if can_act_z:
                self.mail_mark_zimbra_btn.set_callback(self.mark_zimbra_featured)
            else:
                self.mail_mark_zimbra_btn.set_callback(None)
        if not self._trash_zimbra_busy:
            if can_act_z:
                self.mail_trash_zimbra_btn.set_callback(self.trash_zimbra_featured)
            else:
                self.mail_trash_zimbra_btn.set_callback(None)

    def _sync_mail_notifications(
        self,
        *,
        prefix: str,
        source_label: str,
        current_ids: set[str],
        seen: set[str],
        active_id: str,
        last_body: str,
        defer_count: int,
        banner_live: bool,
        latest: Optional[dict],
    ) -> tuple[str, str, int, bool]:
        """Aligne la notification unique d'une source mail sur `current_ids`.

        Préconditions :
          - appelée depuis le main thread (mac_notify n'est pas thread-safe) ;
          - `seen` est muté EN PLACE.
        Retour : `(active_id, last_body, defer_count, banner_live)`.
        Invariants :
          - une seule bannière par source (identifiant fixe `{prefix}-current`) ;
          - on NE notifie PAS un nouveau mail tant que son résumé IA n'est
            pas prêt (corps = résumé) ; garde-fou MAX_NOTIF_DEFER_TICKS pour ne
            jamais bloquer indéfiniment si le résumé n'arrive pas ;
          - tant qu'on diffère, les ids ne sont pas marqués « vus » (on réévalue
            au tick suivant) ;
          - à la sortie d'une émission : `seen == current_ids` ;
          - si le mail actif quitte les non-lus (mark/trash/lecture externe),
            le `latest` suivant est promu même s'il était déjà dans `seen`
            (sinon la file d'attente ne re-notifie jamais).
        """
        new_ids: set[str] = current_ids - seen

        # Oublier les ids disparus du JSON (ne plus les considérer comme "vus").
        seen.difference_update(seen - current_ids)

        notif_id: str = f"{prefix}-current"
        latest_id: str = (
            str(latest.get("id", "")).strip() if isinstance(latest, dict) else ""
        )

        # Slot obsolète : le mail représenté n'est plus non-lu → libérer, puis
        # éventuellement promouvoir le featured suivant dans le même passage.
        if active_id and active_id not in current_ids:
            mac_notify.remove(notif_id)
            active_id = ""
            last_body = ""
            banner_live = False
            defer_count = 0

        # Nouveaux ids, ou slot vacant avec un featured encore non-lu (file
        # d'attente après mark/trash : le suivant était déjà dans seen).
        promote: bool = bool(
            not active_id and latest_id and latest_id in current_ids
        )

        if new_ids or promote:
            summary_ready: bool = (
                isinstance(latest, dict) and bool(str(latest.get("summary", "")).strip())
            )
            has_text: bool = isinstance(latest, dict) and bool(
                str(latest.get("body", "")).strip() or str(latest.get("snippet", "")).strip()
            )
            # Un résumé est ATTENDU si le mail à afficher a du texte mais pas
            # encore de résumé (summarize_mail.py va le calculer sous peu).
            candidate: bool = bool(latest_id) and (
                latest_id in new_ids or promote
            )
            summary_pending: bool = candidate and has_text and not summary_ready

            if summary_pending and defer_count < MAX_NOTIF_DEFER_TICKS:
                # On diffère : ni émission, ni marquage « vu » → réévalué au tick
                # suivant, où le résumé sera probablement disponible.
                return active_id, last_body, defer_count + 1, banner_live

            # Émission (résumé prêt, ou pas de résumé attendu, ou garde-fou atteint).
            title, subtitle, message = _mail_notification_content(source_label, latest)
            mac_notify.deliver(notif_id, title, message, subtitle, actionable=True)
            seen.update(current_ids)
            emitted_id: str = latest_id or (next(iter(new_ids)) if new_ids else "")
            return emitted_id, subtitle, 0, True

        # Résumé IA arrivé après une émission au garde-fou → MAJ du corps.
        # (Pas de deliver si amorce sans bannière : banner_live=False.)
        if (
            active_id
            and isinstance(latest, dict)
            and str(latest.get("id", "")).strip() == active_id
        ):
            title, subtitle, message = _mail_notification_content(source_label, latest)
            if subtitle != last_body:
                if banner_live:
                    mac_notify.deliver(
                        notif_id, title, message, subtitle, actionable=True
                    )
                return active_id, subtitle, 0, banner_live

        return active_id, last_body, defer_count, banner_live

    def _check_notifications(self, data: dict) -> None:
        """Émet/retire les notifications mail, événement et auth gws.

        Invariant : au tout premier passage on AMORCE les id non lus déjà
        présents sans émettre de notification (évite un burst au démarrage) ;
        on peuple aussi `_active_*` sur le featured courant pour ne pas
        le « promouvoir » au tick suivant ; les passages suivants délèguent
        à `_sync_mail_notifications`.
        Auth gws : même amorce (mémoriser sans notif), puis deliver/remove
        sur transition ; rappel via ancre monotonic toutes les
        AUTH_REMIND_INTERVAL s si l'erreur persiste (online). Ids
        `gws-auth-<epoch>` + son pour forcer une alerte visible.
        Collecte : rappel `update-fail-<epoch>` si `_update_collect_failed`
        (échec collecte) toutes les UPDATE_FAIL_REMIND_INTERVAL s.
        """
        gmail_ids: set[str] = {str(x) for x in data.get("unread_gmail_ids", []) if x}
        zimbra_ids: set[str] = {str(x) for x in data.get("unread_zimbra_ids", []) if x}

        if not self._notif_initialized:
            self._seen_gmail_ids = set(gmail_ids)
            self._seen_zimbra_ids = set(zimbra_ids)
            latest_g: object = data.get("latest_unread")
            if isinstance(latest_g, dict):
                gid: str = str(latest_g.get("id", "")).strip()
                if gid and gid in gmail_ids:
                    self._active_gmail_id = gid
                    _, self._active_gmail_body, _ = _mail_notification_content(
                        "Gmail", latest_g
                    )
            latest_z: object = data.get("latest_unread_zimbra")
            if isinstance(latest_z, dict):
                zid: str = str(latest_z.get("id", "")).strip()
                if zid and zid in zimbra_ids:
                    self._active_zimbra_id = zid
                    _, self._active_zimbra_body, _ = _mail_notification_content(
                        "Zimbra", latest_z
                    )
            self._gmail_banner_live = False
            self._zimbra_banner_live = False
            self._notif_initialized = True
        else:
            (
                self._active_gmail_id,
                self._active_gmail_body,
                self._notif_defer_gmail,
                self._gmail_banner_live,
            ) = self._sync_mail_notifications(
                prefix="gmail",
                source_label="Gmail",
                current_ids=gmail_ids,
                seen=self._seen_gmail_ids,
                active_id=self._active_gmail_id,
                last_body=self._active_gmail_body,
                defer_count=self._notif_defer_gmail,
                banner_live=self._gmail_banner_live,
                latest=data.get("latest_unread"),
            )
            (
                self._active_zimbra_id,
                self._active_zimbra_body,
                self._notif_defer_zimbra,
                self._zimbra_banner_live,
            ) = self._sync_mail_notifications(
                prefix="zimbra",
                source_label="Zimbra",
                current_ids=zimbra_ids,
                seen=self._seen_zimbra_ids,
                active_id=self._active_zimbra_id,
                last_body=self._active_zimbra_body,
                defer_count=self._notif_defer_zimbra,
                banner_live=self._zimbra_banner_live,
                latest=data.get("latest_unread_zimbra"),
            )

        event: Optional[dict] = _extract_event(data)
        if event is not None:
            title: str = str(event.get("title", ""))
            start: str = str(event.get("start", ""))
            end: str = str(event.get("end", ""))
            event_id: str = str(event.get("id", "")).strip()
            event_key: tuple[str, str, str, str] = (event_id, title, start, end)
            same_event: bool = (
                self._prev_event_key is not None
                and (
                    event_id == self._prev_event_key[0]
                    if event_id and self._prev_event_key[0]
                    else event_key[1:] == self._prev_event_key[1:]
                )
            )
            if not same_event and self._prev_event_key is not None:
                mac_notify.remove("event-current")
                mac_notify.deliver(
                    "event-current",
                    "📅 Prochain événement",
                    title,
                    format_event_time(start, end) if start else "",
                    actionable=True,
                )
            self._prev_event_key = event_key
        else:
            if self._prev_event_key is not None:
                mac_notify.remove("event-current")
            self._prev_event_key = None

        # Warmup réveil : pas de notif auth (ré-amorce dans _finish_wake_setup).
        if self._wake_ready:
            auth_status: str = str(data.get("gws_auth_status", "ok"))
            offline: bool = data.get("connectivity") == "offline"
            if self._prev_gws_auth_status is None:
                # Amorce : pas de notif au démarrage même si déjà auth_error.
                self._prev_gws_auth_status = auth_status
                if auth_status == "auth_error":
                    self._auth_remind_anchor = time.monotonic()
            elif offline:
                # Hors ligne : pas de notif auth (défense en profondeur).
                # On mémorise quand même le statut pour éviter un burst au retour.
                self._prev_gws_auth_status = auth_status
            elif auth_status != self._prev_gws_auth_status:
                if auth_status == "auth_error":
                    self._deliver_gws_auth_notif()
                elif self._prev_gws_auth_status == "auth_error":
                    mac_notify.remove_prefix(AUTH_NOTIF_PREFIX)
                    self._auth_remind_anchor = None
                self._prev_gws_auth_status = auth_status
            elif (
                not offline
                and auth_status == "auth_error"
                and self._auth_remind_anchor is not None
                and (time.monotonic() - self._auth_remind_anchor)
                >= AUTH_REMIND_INTERVAL
            ):
                self._deliver_gws_auth_notif()

        if (
            self._wake_ready
            and self._update_collect_failed
            and self._update_fail_remind_anchor is not None
            and (time.monotonic() - self._update_fail_remind_anchor)
            >= UPDATE_FAIL_REMIND_INTERVAL
        ):
            self._deliver_update_fail_notif(
                self._update_last_fail_msg or "Collecte impossible"
            )

if __name__ == "__main__":
    _hide_dock_icon()
    DashboardMenubar().run()

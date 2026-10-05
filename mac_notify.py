#!/usr/bin/env python3
"""
Mac Notify — Notifications macOS natives et supprimables

Canal unique du menubar pour toutes les notifications (mails, prochain
événement, feedback « Forcer la mise à jour », et futurs cas sticky).

Émet via `NSUserNotificationCenter` (PyObjC). Chaque notification porte un
`identifier` unique, ce qui permet de la RETIRER plus tard (corbeille du mail
vedette, disparition du mail, ou fin d'un état sticky).

Contrainte connue : `NSUserNotificationCenter` est déprécié depuis macOS 11
mais reste fonctionnel et ne nécessite pas d'autorisation explicite, contrairement
à `UNUserNotificationCenter` (qui exige un bundle `.app` signé). Un script lancé
via `python menubar.py` n'a pas de bundle ; on injecte donc un `CFBundleIdentifier`
au runtime pour que le centre de notifications soit disponible.

Prérequis : `pip install pyobjc-framework-Cocoa`.
Précondition d'appel : main thread uniquement (le centre n'est pas thread-safe).
"""

from typing import Callable, Optional
import os
import time

from Foundation import (
    NSBundle,
    NSObject,
    NSUserNotification,
    NSUserNotificationCenter,
)

_BUNDLE_ID: str = "com.dashboard.menubar"
_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
_ACTIVATE_LOG: str = os.path.join(_SCRIPT_DIR, "logs", "notif-activate.log")

# NSUserNotificationActivationType* (API dépréciée, constantes numériques stables).
_ACTIVATION_NONE: int = 0
_ACTIVATION_CONTENTS_CLICKED: int = 1
_ACTIVATION_ACTION_BUTTON_CLICKED: int = 2

# Retenus explicitement : le centre ne garde qu'une référence faible au délégué.
_activation_handler: Optional[Callable[[str], None]] = None
_delegate_ref: Optional[NSObject] = None


def _log_activate(line: str) -> None:
    """Append diagnostic (clic notif) — no-op si logs/ absent / non inscriptible."""
    try:
        os.makedirs(os.path.dirname(_ACTIVATE_LOG), exist_ok=True)
        with open(_ACTIVATE_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


class _ActivationDelegate(NSObject):
    """Délégué Cocoa : clic corps ou bouton d'action → handler Python."""

    def userNotificationCenter_shouldPresentNotification_(  # noqa: N802
        self, center, notification  # type: ignore[no-untyped-def]
    ) -> bool:
        return True

    def userNotificationCenter_didActivateNotification_(  # noqa: N802
        self, center, notification  # type: ignore[no-untyped-def]
    ) -> None:
        ident = ""
        activation = -1
        try:
            raw_id = notification.identifier()
            ident = str(raw_id) if raw_id else ""
            activation = int(notification.activationType())
        except (AttributeError, TypeError, ValueError) as exc:
            _log_activate(f"activate_error err={exc!r}")
            return

        _log_activate(f"activate id={ident!r} type={activation}")

        # type 0 = None (pas un vrai clic utile) ; le reste (1/2/autres) → handler.
        if activation == _ACTIVATION_NONE:
            return

        handler = _activation_handler
        if handler is None:
            _log_activate("activate_ignored no_handler")
            return
        if not ident:
            _log_activate("activate_ignored empty_id")
            return
        try:
            handler(ident)
            _log_activate(f"handler_ok id={ident!r}")
        except Exception as exc:  # noqa: BLE001 — log diagnostic, ne pas crasher Cocoa
            _log_activate(f"handler_error id={ident!r} err={exc!r}")


def set_activation_handler(
    handler: Optional[Callable[[str], None]],
) -> bool:
    """Branche (ou retire) le callback au clic / bouton d'action.

    Précondition : main thread. `handler` reçoit l'`identifier` de la notif
    activée. Passer `None` pour désinstaller. Retour : False si le centre
    est indisponible.
    """
    global _activation_handler, _delegate_ref
    center: Optional[NSUserNotificationCenter] = _center()
    if center is None:
        _log_activate("set_handler center_unavailable")
        return False
    _activation_handler = handler
    if handler is None:
        center.setDelegate_(None)
        _delegate_ref = None
        _log_activate("set_handler cleared")
        return True
    if _delegate_ref is None:
        _delegate_ref = _ActivationDelegate.alloc().init()
    center.setDelegate_(_delegate_ref)
    _log_activate("set_handler installed")
    return True


def _ensure_delegate_bound(center: NSUserNotificationCenter) -> None:
    """Ré-applique le délégué (défense si quelque chose l'a écrasé)."""
    if _activation_handler is None:
        return
    if _delegate_ref is None:
        return
    center.setDelegate_(_delegate_ref)


def _ensure_bundle_id() -> None:
    """Injecte un CFBundleIdentifier si le process n'en a pas.

    Précondition : aucune (idempotent).
    Invariant : ne réécrit jamais un bundle id déjà défini (cas d'une vraie
    `.app` packagée) ; ne fait qu'ajouter le nôtre quand il manque.
    """
    bundle: Optional[NSBundle] = NSBundle.mainBundle()
    if bundle is None:
        return
    info: Optional[dict] = bundle.infoDictionary()
    if info is None:
        return
    if not info.get("CFBundleIdentifier"):
        info["CFBundleIdentifier"] = _BUNDLE_ID


def _center() -> Optional[NSUserNotificationCenter]:
    """Retourne le centre de notifications, ou None s'il est indisponible.

    Invariant : tente d'abord d'injecter le bundle id, car sans lui
    `defaultUserNotificationCenter()` renvoie None.
    """
    _ensure_bundle_id()
    return NSUserNotificationCenter.defaultUserNotificationCenter()


def deliver(
    identifier: str,
    title: str,
    message: str,
    subtitle: str = "",
    *,
    sound: bool = False,
    actionable: bool = False,
) -> bool:
    """Affiche une notification portant `identifier` (unique, pour suppression).

    Préconditions : `identifier` et `title` non vides ; à appeler depuis le
    main thread (le centre de notifications n'est pas thread-safe).
    `sound` : joue le son système par défaut (aide à attirer l'attention quand
    macOS livre la notif au Centre sans bannière).
    `actionable` : bouton d'action explicite « Ouvrir » (le libellé système
    « Afficher » n'appelle pas toujours le délégué sur les bannières Python).
    Retour : True si remise au centre, False si le centre est indisponible
    (process sans bundle utilisable, p. ex.).
    """
    center: Optional[NSUserNotificationCenter] = _center()
    if center is None:
        return False
    _ensure_delegate_bound(center)
    notif: NSUserNotification = NSUserNotification.alloc().init()
    notif.setIdentifier_(identifier)
    notif.setTitle_(title)
    if subtitle:
        notif.setSubtitle_(subtitle)
    notif.setInformativeText_(message)
    if actionable:
        notif.setHasActionButton_(True)
        notif.setActionButtonTitle_("Ouvrir")
    if sound:
        # Constante Cocoa ; string acceptée aussi par setSoundName_.
        notif.setSoundName_("NSUserNotificationDefaultSoundName")
    center.deliverNotification_(notif)
    return True


def remove(identifier: str) -> None:
    """Retire la notification affichée dont l'identifier correspond.

    Précondition : à appeler depuis le main thread.
    Invariant : on itère les notifications réellement délivrées et on retire
    l'objet correspondant (plus robuste que reconstruire une notification),
    sans erreur si aucune ne correspond (no-op silencieux volontaire ici, car
    « rien à retirer » est un état normal, pas un crash silencieux).
    """
    center: Optional[NSUserNotificationCenter] = _center()
    if center is None:
        return
    delivered: Optional[list] = center.deliveredNotifications()
    if not delivered:
        return
    for notif in delivered:
        if notif.identifier() == identifier:
            center.removeDeliveredNotification_(notif)


def remove_prefix(prefix: str) -> None:
    """Retire toutes les notifications dont l'identifier commence par `prefix`.

    Précondition : à appeler depuis le main thread. Utile pour les rappels
    auth qui utilisent un id horodaté (`gws-auth-<epoch>`).
    """
    if not prefix:
        return
    center: Optional[NSUserNotificationCenter] = _center()
    if center is None:
        return
    delivered: Optional[list] = center.deliveredNotifications()
    if not delivered:
        return
    for notif in delivered:
        ident = notif.identifier()
        if ident and ident.startswith(prefix):
            center.removeDeliveredNotification_(notif)


def remove_all() -> None:
    """Retire toutes les notifications délivrées par ce process.

    Précondition : à appeler depuis le main thread.
    """
    center: Optional[NSUserNotificationCenter] = _center()
    if center is None:
        return
    center.removeAllDeliveredNotifications()


def _selftest() -> None:
    """Test manuel : notif actionable ; cliquer « Ouvrir » puis Ctrl-C.

    Lancer : `venv/bin/python3 mac_notify.py`
    Observer `logs/notif-activate.log` et l'ouverture du navigateur.
    """
    import subprocess

    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
    from PyObjCTools import AppHelper

    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )

    def _on_activate(ident: str) -> None:
        print(f"handler called: {ident}")
        subprocess.run(
            ["open", "https://mail.google.com/mail/u/0/#inbox"],
            check=False,
        )

    ok_handler: bool = set_activation_handler(_on_activate)
    print(f"set_activation_handler -> {ok_handler}")
    ok: bool = deliver(
        "selftest-click",
        title="Test clic mac_notify",
        message="Cliquer « Ouvrir » (pas seulement Afficher). Ctrl-C pour quitter.",
        subtitle="Selftest",
        actionable=True,
    )
    print(f"deliver -> {ok}")
    if not ok:
        print("Centre indisponible : bundle id non pris en compte ?")
        return
    print(f"Log: {_ACTIVATE_LOG}")
    print("En attente du clic (run loop)…")
    AppHelper.runConsoleEventLoop()


if __name__ == "__main__":
    _selftest()

#!/usr/bin/env python3
"""
Mac Notify — Notifications macOS natives et supprimables

Émet des notifications via `NSUserNotificationCenter` (PyObjC) plutôt que via
`osascript display notification`. L'intérêt : chaque notification porte un
`identifier` unique, ce qui permet de la RETIRER plus tard (au clic « marquer
comme lu » ou quand le mail correspondant disparaît des données).

Contrainte connue : `NSUserNotificationCenter` est déprécié depuis macOS 11
mais reste fonctionnel et ne nécessite pas d'autorisation explicite, contrairement
à `UNUserNotificationCenter` (qui exige un bundle `.app` signé). Un script lancé
via `python menubar.py` n'a pas de bundle ; on injecte donc un `CFBundleIdentifier`
au runtime pour que le centre de notifications soit disponible.

Prérequis : `pip install pyobjc-framework-Cocoa`.
"""

from typing import Optional

from Foundation import (
    NSBundle,
    NSUserNotification,
    NSUserNotificationCenter,
)

_BUNDLE_ID: str = "com.dashboard.menubar"


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


def deliver(identifier: str, title: str, message: str, subtitle: str = "") -> bool:
    """Affiche une notification portant `identifier` (unique, pour suppression).

    Préconditions : `identifier` et `title` non vides ; à appeler depuis le
    main thread (le centre de notifications n'est pas thread-safe).
    Retour : True si remise au centre, False si le centre est indisponible
    (process sans bundle utilisable, p. ex.).
    """
    center: Optional[NSUserNotificationCenter] = _center()
    if center is None:
        return False
    notif: NSUserNotification = NSUserNotification.alloc().init()
    notif.setIdentifier_(identifier)
    notif.setTitle_(title)
    if subtitle:
        notif.setSubtitle_(subtitle)
    notif.setInformativeText_(message)
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


def remove_all() -> None:
    """Retire toutes les notifications délivrées par ce process.

    Précondition : à appeler depuis le main thread.
    """
    center: Optional[NSUserNotificationCenter] = _center()
    if center is None:
        return
    center.removeAllDeliveredNotifications()


def _selftest() -> None:
    """Test manuel : émet une notif, attend, puis la retire.

    Lancer avec `python mac_notify.py` et observer le centre de notifications.
    """
    import time

    ok: bool = deliver(
        "selftest-1",
        title="Test mac_notify",
        message="Cette notification doit disparaître dans 4 s.",
        subtitle="Selftest",
    )
    print(f"deliver -> {ok}")
    if not ok:
        print("Centre indisponible : bundle id non pris en compte ?")
        return
    time.sleep(4)
    remove("selftest-1")
    print("remove -> notification retirée (vérifie le centre).")


if __name__ == "__main__":
    _selftest()

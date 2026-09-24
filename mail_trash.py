#!/usr/bin/env python3
"""
Mail Trash — Mise à la corbeille du mail vedette (Gmail / Zimbra)

Cible uniquement l'id fourni (celui de `latest_unread` / `latest_unread_zimbra`
dans dashboard.json). Gmail via `gws` (messages.trash) ; Zimbra via IMAP
(COPY vers Trash puis suppression de l'INBOX).

Précondition d'appel : hors du main thread menubar (réseau / subprocess).
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional

import imaplib

from zimbra_unread import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TIMEOUT

# Dossiers Trash courants (Zimbra UL / IMAP générique).
_ZIMBRA_TRASH_CANDIDATES: tuple[str, ...] = (
    "Trash",
    "Corbeille",
    "INBOX.Trash",
    "INBOX/Trash",
)


def _gws_env() -> dict:
    env: dict = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
    env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = os.path.expanduser("~/.config/gws")
    return env


def trash_gmail_message(message_id: str, *, timeout: int = 30) -> None:
    """Déplace le message Gmail `message_id` vers la corbeille (API trash).

    Précondition : `message_id` non vide.
    Lève RuntimeError si gws échoue (auth, scope, id inconnu, réseau).
    """
    if not message_id:
        raise ValueError("message_id requis")
    proc: subprocess.CompletedProcess[str] = subprocess.run(
        [
            "gws",
            "gmail",
            "users",
            "messages",
            "trash",
            "--params",
            json.dumps({"userId": "me", "id": message_id}),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_gws_env(),
    )
    if proc.returncode == 0:
        return
    detail: str = (proc.stderr or proc.stdout or "").strip()
    if not detail:
        detail = f"gws exit {proc.returncode}"
    # Tronquer : pas de corps de mail dans les erreurs remontées à l'UI.
    raise RuntimeError(detail[:200])


def _list_trash_mailbox(conn: imaplib.IMAP4_SSL) -> Optional[str]:
    """Retourne le nom du dossier marqué \\Trash, ou None."""
    status, data = conn.list()
    if status != "OK" or not data:
        return None
    for raw in data:
        if not raw:
            continue
        line: str = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        if "\\Trash" not in line:
            continue
        # Dernier segment entre guillemets = nom du mailbox.
        parts: list[str] = line.split('"')
        if len(parts) >= 2 and parts[-2]:
            return parts[-2]
    return None


def trash_zimbra_message(
    uid: str,
    user: str,
    password: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: int = DEFAULT_TIMEOUT,
    mailbox: str = "INBOX",
) -> None:
    """Copie le message UID vers Trash puis le retire de l'INBOX.

    Préconditions : uid / user / password non vides.
    Invariants :
      - écriture IMAP ciblée (pas readonly) ; le mail reste récupérable dans Trash ;
      - on pose \\Seen **avant** le COPY pour que la copie n'arrive pas
        non lue dans la corbeille (flags IMAP souvent recopiés tels quels).
    """
    if not uid or not user or not password:
        raise ValueError("uid, user et password requis")

    conn: imaplib.IMAP4_SSL = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    try:
        conn.login(user, password)
        status, _ = conn.select(mailbox, readonly=False)
        if status != "OK":
            raise RuntimeError(f"SELECT {mailbox} a échoué : {status}")

        # Marquer lu avant COPY : sinon Trash hérite souvent de UNSEEN.
        seen_status, _ = conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        if seen_status != "OK":
            raise RuntimeError(f"STORE \\Seen a échoué : {seen_status}")

        trash_name: Optional[str] = _list_trash_mailbox(conn)
        candidates: list[str] = []
        if trash_name:
            candidates.append(trash_name)
        for name in _ZIMBRA_TRASH_CANDIDATES:
            if name not in candidates:
                candidates.append(name)

        copied: bool = False
        last_status: str = ""
        for trash in candidates:
            last_status, _ = conn.uid("COPY", uid, trash)
            if last_status == "OK":
                copied = True
                break
        if not copied:
            raise RuntimeError(
                f"COPY vers Trash a échoué (dernier statut : {last_status or 'n/a'})"
            )

        store_status, _ = conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        if store_status != "OK":
            raise RuntimeError(f"STORE \\Deleted a échoué : {store_status}")
        conn.expunge()
    finally:
        try:
            conn.logout()
        except Exception:
            pass

#!/usr/bin/env python3
"""Détection des erreurs OAuth du CLI gws (token révoqué, expiré, absent)."""

from __future__ import annotations

import json
import subprocess
from typing import Optional

# Marqueurs observés dans stdout/stderr de gws (codes 1=api, 2=auth).
_AUTH_MARKERS: tuple[str, ...] = (
    "invalid_grant",
    "token has been expired or revoked",
    "token expired or revoked",
    "error[auth]:",
    '"reason": "autherror"',
    "autherror",
    "no credentials provided",
    "invalid authentication credentials",
    "run `gws auth login`",
    "gws auth login",
    "gws auth refresh",
)


def parse_gws_auth_status_output(stdout: str) -> Optional[dict]:
    """Extrait le premier objet JSON de la sortie de `gws auth status`.

    Précondition : stdout peut contenir du bruit avant/après le JSON.
    Retour : dict parsé, ou None si aucun objet JSON exploitable.
    """
    if not stdout:
        return None
    start: int = stdout.find("{")
    if start < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(stdout[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def auth_status_indicates_error(payload: dict) -> bool:
    """True si le JSON de `gws auth status` prouve un problème OAuth.

    Invariant : absence de preuve (champs manquants) → False (indécis),
    pour ne pas court-circuiter la collecte sur un statut ambigu.
    """
    if payload.get("token_valid") is False:
        return True
    # Existence credentials : seulement si gws a fourni au moins un des champs.
    if (
        "encrypted_credentials_exists" in payload
        or "plain_credentials_exists" in payload
    ):
        has_enc: bool = bool(payload.get("encrypted_credentials_exists"))
        has_plain: bool = bool(payload.get("plain_credentials_exists"))
        if not has_enc and not has_plain:
            return True
    # OAuth sans refresh durable et sans preuve que le token access est OK.
    if (
        "has_refresh_token" in payload
        and payload.get("has_refresh_token") is False
        and not bool(payload.get("plain_credentials_exists"))
        and payload.get("token_valid") is not True
    ):
        return True
    return False


def is_gws_auth_failure(proc: subprocess.CompletedProcess[str]) -> bool:
    """True si la sortie gws indique un problème d'authentification OAuth.

    Précondition : proc issu d'un appel gws terminé (returncode peut être ≠ 0).
    """
    if proc.returncode == 0:
        return False
    # gws documente le code 2 pour les erreurs d'auth.
    if proc.returncode == 2:
        return True
    combined: str = (proc.stderr or "") + (proc.stdout or "")
    lower: str = combined.lower()
    return any(marker in lower for marker in _AUTH_MARKERS)


def derive_gws_auth_status(
    *,
    cal_ok: bool,
    gmail_count_ok: bool,
    cal_proc: Optional[subprocess.CompletedProcess[str]],
    gmail_proc: Optional[subprocess.CompletedProcess[str]],
    previous_status: str = "ok",
) -> str:
    """Dérive gws_auth_status pour le JSON du dashboard.

    Invariant : au moins un appel gws réussi (cal ou gmail list) → "ok".
    Échec avec marqueurs OAuth → "auth_error".
    Sinon on conserve le statut précédent (ex. timeout réseau sans preuve auth).
    """
    if cal_ok or gmail_count_ok:
        return "ok"
    cal_auth: bool = (
        cal_proc is not None
        and not cal_ok
        and is_gws_auth_failure(cal_proc)
    )
    gmail_auth: bool = (
        gmail_proc is not None
        and not gmail_count_ok
        and is_gws_auth_failure(gmail_proc)
    )
    if cal_auth or gmail_auth:
        return "auth_error"
    if previous_status in ("ok", "auth_error"):
        return previous_status
    return "ok"

#!/usr/bin/env python3
"""Détection des erreurs OAuth du CLI gws (token révoqué, expiré, absent)."""

from __future__ import annotations

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

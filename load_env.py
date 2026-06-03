#!/usr/bin/env python3
"""
Charge les variables depuis un fichier `.env` à la racine du projet.

Comportement (aligné sur python-dotenv) :
  - ne remplace pas une variable déjà définie dans l'environnement ;
  - lignes vides et commentaires (#) ignorés ;
  - valeurs entre guillemets simples ou doubles retirées.

Précondition : appeler une fois au démarrage de chaque point d'entrée
(menubar, dashboard_update, zimbra_unread) avant tout os.environ.get.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_PROJECT_DIR: Path = Path(__file__).resolve().parent
DEFAULT_ENV_FILE: Path = _PROJECT_DIR / ".env"


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_line(line: str) -> Optional[tuple[str, str]]:
    """Retourne (clé, valeur) ou None si la ligne est ignorée."""
    stripped: str = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None
    key, _, raw_value = stripped.partition("=")
    key = key.strip()
    if not key:
        return None
    value: str = _strip_quotes(raw_value.strip())
    return key, value


def load_project_env(
    env_path: Optional[Path] = None,
    *,
    override: bool = False,
) -> bool:
    """Charge `.env` dans os.environ.

    Retourne True si le fichier existe et a été lu, False sinon.
    """
    path: Path = env_path if env_path is not None else DEFAULT_ENV_FILE
    if not path.is_file():
        return False

    with path.open(encoding="utf-8") as f:
        for line in f:
            parsed = _parse_line(line)
            if parsed is None:
                continue
            key, value = parsed
            if not override and key in os.environ:
                continue
            os.environ[key] = value
    return True

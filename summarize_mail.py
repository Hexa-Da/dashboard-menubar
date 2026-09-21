#!/usr/bin/env python3
"""
Summarize Mail — Résume les derniers mails non lus via Azure OpenAI

Lit dashboard.json et, pour chaque boîte sans résumé existant :
  - latest_unread (Gmail) ;
  - latest_unread_zimbra (Zimbra).

Demande un résumé court à Azure OpenAI (chat completions) et écrit
le champ `summary` dans le JSON.

Appelé par dashboard_update.py après chaque collecte (ou manuellement).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import quote, urlparse

from load_env import load_project_env

load_project_env()

DATA_FILE: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.json")
MAX_BODY_CHARS: int = 3000
MAX_SUMMARY_CHARS: int = 100
HTTP_TIMEOUT_S: int = 30
DEFAULT_API_VERSION: str = "2024-12-01-preview"


def _write_json_atomic(path: str, data: dict) -> None:
    """Écrit le JSON de façon atomique (tmp + os.replace) : un lecteur
    concurrent (le menubar, toutes les 10 s) ne voit jamais un fichier tronqué."""
    tmp: str = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _azure_chat_target(
    endpoint: str,
    deployment: str,
    api_version: str,
) -> Optional[tuple[str, bool]]:
    """Construit l'URL chat completions.

    Retourne (url, model_in_body) :
      - Foundry projet `/api/projects/…` ou host `services.ai.azure.com` → API v1
        (`model` dans le body) ;
      - sinon Azure OpenAI classique → `/openai/deployments/{name}/…`.
    """
    parsed = urlparse(endpoint.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    origin: str = f"{parsed.scheme}://{parsed.netloc}"
    path: str = parsed.path.rstrip("/")
    marker: str = "/api/projects/"

    if marker in path:
        after: str = path.split(marker, 1)[1]
        project: str = after.split("/", 1)[0]
        if not project:
            return None
        url = f"{origin}{marker}{project}/openai/v1/chat/completions"
        return url, True

    if parsed.netloc.endswith("services.ai.azure.com") or path.startswith("/openai/v1"):
        return f"{origin}/openai/v1/chat/completions", True

    url = (
        f"{origin}/openai/deployments/{quote(deployment, safe='')}"
        f"/chat/completions?api-version={quote(api_version, safe='')}"
    )
    return url, False


def _azure_chat(prompt: str) -> Optional[str]:
    """Appelle Azure OpenAI chat completions ; retourne le contenu ou None."""
    api_key: Optional[str] = os.environ.get("AZURE_OPENAI_API_KEY")
    endpoint: Optional[str] = os.environ.get("AZURE_OPENAI_ENDPOINT")
    deployment: Optional[str] = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    api_version: str = os.environ.get("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION)

    if not api_key or not endpoint or not deployment:
        print(
            "Azure OpenAI non configuré : "
            "AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT "
            "requis dans .env",
            file=sys.stderr,
        )
        return None

    target = _azure_chat_target(endpoint, deployment, api_version)
    if target is None:
        print("AZURE_OPENAI_ENDPOINT invalide (attendu https://…)", file=sys.stderr)
        return None
    url, model_in_body = target

    payload: dict = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Tu es un assistant de résumé de mails. "
                    "Réponds UNIQUEMENT avec le résumé, sans aucun préfixe ni commentaire."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": 200,
    }
    if model_in_body:
        # API Foundry v1 : le modèle est dans le body ; gpt-5 sans effort
        # minimal peut consommer tout le budget en reasoning (contenu vide).
        payload["model"] = deployment
        payload["reasoning_effort"] = "minimal"

    body: bytes = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "api-key": api_key,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            raw: bytes = response.read()
    except urllib.error.HTTPError as e:
        detail: str = e.read().decode("utf-8", errors="replace").strip()
        print(f"Azure OpenAI HTTP {e.code}: {detail or e.reason}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"Azure OpenAI network error: {e.reason}", file=sys.stderr)
        return None
    except (TimeoutError, socket.timeout):
        print("Azure OpenAI timeout", file=sys.stderr)
        return None

    try:
        data: dict = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"Azure OpenAI invalid JSON: {e}", file=sys.stderr)
        return None

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        print("Azure OpenAI: empty choices", file=sys.stderr)
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        print("Azure OpenAI: missing message", file=sys.stderr)
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        print("Azure OpenAI: empty content", file=sys.stderr)
        return None
    return content.strip()


def _summarize_one(latest: dict) -> bool:
    """Résume un mail (dict avec from/subject/body/snippet) et écrit `summary`.

    Préconditions : latest est un dict mutable.
    Retour : True si un résumé a été écrit dans latest, False sinon.
    """
    body: str = latest.get("body", "").strip()
    if not body:
        body = latest.get("snippet", "").strip()
    if not body:
        return False

    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "…"

    sender: str = latest.get("from", "")
    subject: str = latest.get("subject", "")

    prompt: str = (
        "Résume ce mail en UNE phrase complète de 60 caractères max, en français. "
        "Va droit au but, pas de « Ce mail » ni « L'email ».\n\n"
        f"De : {sender}\n"
        f"Objet : {subject}\n"
        f"Corps :\n{body}"
    )

    summary: Optional[str] = _azure_chat(prompt)
    if not summary:
        return False

    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[: MAX_SUMMARY_CHARS - 1] + "…"

    latest["summary"] = summary
    print("OK — summary generated")
    return True


def main() -> None:
    if not os.path.exists(DATA_FILE):
        print("No dashboard file found", file=sys.stderr)
        return

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data: dict = json.load(f)

    changed: bool = False
    # On résume Gmail et Zimbra, mais seulement si pas déjà fait.
    for key in ("latest_unread", "latest_unread_zimbra"):
        latest: object = data.get(key)
        if isinstance(latest, dict) and not latest.get("summary"):
            if _summarize_one(latest):
                changed = True

    if changed:
        _write_json_atomic(DATA_FILE, data)
    else:
        print("Nothing to summarize")


if __name__ == "__main__":
    main()

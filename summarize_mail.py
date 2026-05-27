#!/usr/bin/env python3
"""
Summarize Mail — Résume le dernier mail via OpenClaw

Lit le body du dernier mail non lu depuis dashboard.json,
demande un résumé court à OpenClaw (openclaw infer model run),
et écrit le résultat dans le champ `summary` du JSON.

Conçu pour être appelé par dashboard_update.py après chaque collecte.
"""

import json
import os
import subprocess
import sys

DATA_FILE: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.json")
MODEL: str = "azure/o4-mini"
MAX_BODY_CHARS: int = 3000
MAX_SUMMARY_CHARS: int = 100


def main() -> None:
    if not os.path.exists(DATA_FILE):
        print("No dashboard file found", file=sys.stderr)
        return

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data: dict = json.load(f)

    latest: object = data.get("latest_unread")
    if not isinstance(latest, dict):
        print("No unread mail to summarize")
        return

    body: str = latest.get("body", "").strip()
    if not body:
        body = latest.get("snippet", "").strip()
    if not body:
        print("No mail body to summarize")
        return

    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "…"

    sender: str = latest.get("from", "")
    subject: str = latest.get("subject", "")

    prompt: str = (
        "Tu es un assistant de résumé de mails. "
        "Réponds UNIQUEMENT avec le résumé, sans aucun préfixe ni commentaire.\n"
        "Résume ce mail en UNE phrase complète de 60 caractères max, en français. "
        "Va droit au but, pas de « Ce mail » ni « L'email ».\n\n"
        f"De : {sender}\n"
        f"Objet : {subject}\n"
        f"Corps :\n{body}"
    )

    try:
        result: subprocess.CompletedProcess = subprocess.run(
            [
                "openclaw", "infer", "model", "run",
                "--prompt", prompt,
                "--model", MODEL,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")},
        )
        if result.returncode != 0:
            print(f"OpenClaw error: {result.stderr.strip()}", file=sys.stderr)
            return

        # Strip openclaw CLI metadata lines from output
        raw_lines: list[str] = result.stdout.strip().split("\n")
        content_lines: list[str] = [
            line for line in raw_lines
            if not line.strip().startswith(("model.run via", "provider:", "model:", "outputs:"))
        ]
        summary: str = "\n".join(content_lines).strip()
        if not summary:
            print("Empty summary returned", file=sys.stderr)
            return

        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[: MAX_SUMMARY_CHARS - 1] + "…"

        latest["summary"] = summary

        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        print(f"OK — {summary}")

    except subprocess.TimeoutExpired:
        print("OpenClaw timeout", file=sys.stderr)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

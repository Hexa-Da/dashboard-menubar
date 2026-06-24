#!/usr/bin/env python3
"""
Zimbra Unread — Accès IMAP à la messagerie étudiante UL

Récupère le nombre de mails non lus et le détail du dernier non lu sur la
messagerie Zimbra de l'Université de Lorraine via IMAP (SSL).

Pourquoi IMAP et pas SOAP/CAS : la connexion web passe par le SSO CAS, donc
pas de login/mot de passe direct côté navigateur. IMAP accepte l'identifiant
+ mot de passe ENT et reste le chemin validé pour un script.

Aucune dépendance externe (imaplib, email font partie de la stdlib).

Identifiants : fichier `.env` à la racine du projet (voir `.env.example`)
ou variables d'environnement (jamais en dur, jamais committées).

    cp .env.example .env   # puis éditer .env
    python3 zimbra_unread.py

Surcharges optionnelles : ZIMBRA_IMAP_HOST, ZIMBRA_IMAP_PORT.
"""

import email
import email.message
import imaplib
import os
import sys
from email.header import decode_header
from typing import Optional, TypedDict

DEFAULT_HOST: str = "mail.etu.univ-lorraine.fr"
DEFAULT_PORT: int = 993
DEFAULT_TIMEOUT: int = 20  # secondes : évite un blocage IMAP indéfini
MAX_BODY_CHARS: int = 3000  # aligné sur Gmail (summarize_mail.py)
MAX_SNIPPET_CHARS: int = 200
MAX_SUBJECT_CHARS: int = 80


class ZimbraLatestMail(TypedDict):
    """Détail du dernier mail non lu.

    `id` est l'UID IMAP (string, stable) : sert à détecter
    « même mail qu'avant » et réutiliser un résumé déjà calculé.
    """
    id: str
    from_: str
    subject: str
    snippet: str
    body: str


def _decode_header(value: Optional[str]) -> str:
    """Décode un en-tête MIME (=?utf-8?...?=) en str lisible.

    Invariant : retourne "" si value est None/vide.
    """
    if not value:
        return ""
    parts: list = decode_header(value)
    out: list[str] = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _extract_text_body(msg: email.message.Message) -> str:
    """Extrait le premier corps text/plain d'un message email.

    Invariant : retourne "" si aucun text/plain exploitable.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                disp = str(part.get("Content-Disposition") or "")
                if "attachment" in disp.lower():
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return ""


def fetch_zimbra_mailbox(
    user: str,
    password: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    mailbox: str = "INBOX",
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, Optional[ZimbraLatestMail], list[str]]:
    """Retourne (nb_non_lus, dernier_non_lu | None, liste_uids_non_lus).

    Préconditions : user et password non vides.
    Invariants :
      - la boîte est ouverte en readonly (BODY.PEEK) → ne marque rien lu ;
      - on identifie les mails par UID (stable), pas par numéro de séquence ;
      - la liste d'UID retournée est en str (alignée sur `ZimbraLatestMail.id`),
        triée par UID croissant (le dernier = le plus récent) ;
      - la connexion IMAP est toujours fermée en finally.
    """
    if not user or not password:
        raise ValueError("user et password requis")

    conn: imaplib.IMAP4_SSL = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    try:
        conn.login(user, password)
        conn.select(mailbox, readonly=True)

        # UID SEARCH : les UID sont stables, contrairement aux numéros de
        # séquence qui se décalent quand un mail arrive ou est expurgé.
        status, data = conn.uid("search", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"UID SEARCH UNSEEN a échoué : {status}")

        uids: list[bytes] = data[0].split()
        count: int = len(uids)
        uid_strs: list[str] = [u.decode("ascii", errors="replace") for u in uids]
        if count == 0:
            return 0, None, []

        # UID croissants → le plus élevé = le plus récent.
        last_uid: bytes = uids[-1]
        last_uid_str: str = last_uid.decode("ascii", errors="replace")

        # BODY.PEEK[] récupère tout sans marquer comme lu, on parse ensuite.
        status, msg_data = conn.uid("fetch", last_uid, "(BODY.PEEK[])")
        if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            # On a le compte mais pas le détail : renvoyer au moins le count + uids.
            return count, None, uid_strs

        raw: bytes = msg_data[0][1]
        msg: email.message.Message = email.message_from_bytes(raw)

        from_val: str = _decode_header(msg.get("From"))
        subject_val: str = _decode_header(msg.get("Subject"))[:MAX_SUBJECT_CHARS]
        body: str = _extract_text_body(msg)
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "…"
        snippet: str = " ".join(body.split())[:MAX_SNIPPET_CHARS]

        latest: ZimbraLatestMail = {
            "id": last_uid_str,
            "from_": from_val,
            "subject": subject_val,
            "snippet": snippet,
            "body": body,
        }
        return count, latest, uid_strs
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def main() -> None:
    from load_env import load_project_env

    load_project_env()
    user: Optional[str] = os.environ.get("ZIMBRA_USER")
    password: Optional[str] = os.environ.get("ZIMBRA_PASS")
    host: str = os.environ.get("ZIMBRA_IMAP_HOST", DEFAULT_HOST)
    port: int = int(os.environ.get("ZIMBRA_IMAP_PORT", DEFAULT_PORT))

    if not user or not password:
        print(
            "Erreur : définis ZIMBRA_USER et ZIMBRA_PASS dans .env ou l'environnement.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        count, latest, _uids = fetch_zimbra_mailbox(user, password, host=host, port=port)
    except imaplib.IMAP4.error as e:
        print(f"Erreur IMAP (login/serveur ?) : {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Erreur : {e}", file=sys.stderr)
        sys.exit(3)

    print(f"Mails non lus : {count}")
    if latest:
        print(f"  De     : {latest['from_']}")
        print(f"  Objet  : {latest['subject']}")
        if latest["snippet"]:
            print(f"  Extrait: {latest['snippet']}")


if __name__ == "__main__":
    main()

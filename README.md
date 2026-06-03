# Dashboard Menubar

Une app macOS qui vit dans la barre de menus et affiche en un coup d'oeil :

- **Google Calendar** : prochain événement (titre, horaire, lieu)
- **Gmail** : nombre de mails non lus + expéditeur et résumé IA du dernier mail (via [OpenClaw](https://github.com/nicholasgasior/openclaw))
- **Zimbra (UL)** : nombre de mails non lus + expéditeur et résumé IA du dernier mail (via IMAP)

![macOS](https://img.shields.io/badge/macOS-compatible-blue)
![Python](https://img.shields.io/badge/Python-3.9+-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

## Fonctionnement

```
menubar.py (toujours actif via LaunchAgent)
   ├──(toutes les 2 min)── dashboard_update.py ──> dashboard.json
   │                            ├── gws ── Calendar + Gmail
   │                            ├── zimbra_unread (IMAP, .env) ── Zimbra UL
   │                            └── summarize_mail.py ── OpenClaw (Gmail + Zimbra)
   └──(toutes les 10 s)── lit dashboard.json ──> barre de menus + badge (Gmail+Zimbra)
```

- **`menubar.py`** est le chef d'orchestre : un seul process, maintenu en vie par un LaunchAgent (`KeepAlive`). Il déclenche la collecte toutes les 2 min, relit `dashboard.json` toutes les 10 s, affiche Gmail et Zimbra en sections distinctes, et le chiffre sur la cloche = **Gmail + Zimbra**.
- **`load_env.py`** charge le fichier `.env` au démarrage (stdlib, pas de dépendance `python-dotenv`).
- **`dashboard_update.py`** appelle les APIs Google Calendar et Gmail via [`gws`](https://github.com/nicholasgasior/gws), interroge Zimbra via IMAP, et écrit le résultat dans `dashboard.json` à la racine du projet.
- **`gws_errors.py`** détecte les échecs OAuth de gws (token révoqué, `invalid_grant`, credentials absents) à partir de la sortie du CLI.
- **`zimbra_unread.py`** se connecte à la messagerie Zimbra de l'UL en IMAP (SSL, lecture seule), compte les non-lus et récupère le dernier sans le marquer comme lu.
- **`summarize_mail.py`** envoie le corps du dernier mail (Gmail et Zimbra) à OpenClaw et écrit un résumé d'une phrase dans le JSON.
- Le menubar pilote les mises à jour (plus de LaunchAgent dédié à la collecte) : robuste face aux veilles fréquentes (anti-App-Nap + refresh au réveil).

## Installation

### Prérequis

- macOS
- Python 3.9+
- [gws](https://github.com/nicholasgasior/gws) configuré avec un compte Google (`gws auth login`)
- [OpenClaw](https://github.com/nicholasgasior/openclaw) pour le résumé IA des mails
- [Homebrew](https://brew.sh/) (recommandé)

#### Zimbra (Université de Lorraine)

L'accès se fait en **IMAP**, pas par la connexion web (qui passe par le SSO **CAS**). Le mot de passe ENT sert ici uniquement pour IMAP.

- IMAP doit être activé sur le compte.
- Identifiants via un fichier **`.env`** à la racine.

### Setup

```bash
# Cloner le repo
git clone https://github.com/Hexa-Da/dashboard-menubar.git ~/Documents/dashboard-menubar
cd ~/Documents/dashboard-menubar

# Créer le virtualenv
python3 -m venv venv
source venv/bin/activate
pip install rumps pyobjc-framework-Cocoa

# Tester le script de mise à jour
python3 dashboard_update.py

# Lancer la menubar
python3 menubar.py
```

### LaunchAgent (démarrage automatique)

L'app tourne via un **seul LaunchAgent** macOS (`~/Library/LaunchAgents/`) :

- `com.paulantoine.dashboard-menubar.plist` — maintient `menubar.py` en vie (`KeepAlive`). Le menubar déclenche lui-même `dashboard_update.py` toutes les 2 minutes.

```bash
launchctl load ~/Library/LaunchAgents/com.paulantoine.dashboard-menubar.plist
```

Les logs de collecte sont écrits dans `logs/dashboard-update.log`.

## Menu

| Item | Action au clic |
|------|----------------|
| Prochain événement | Ouvre Google Calendar |
| Gmail : X non lus | Ouvre Gmail |
| Zimbra : X non lus | Ouvre le webmail UL |
| Dernière mise à jour | Ouvre le fichier JSON |
| Marquer Gmail comme lu | Masque le compteur Gmail (UI uniquement) |
| Marquer Zimbra comme lu | Masque le compteur Zimbra (UI uniquement) |
| Forcer la mise à jour | Appelle Google + Zimbra et réécrit le JSON |
| Quitter | Ferme l'app |

### Indicateurs de connexion dans le menu

Les suffixes sont ajoutés aux lignes **Calendar**, **Gmail** et **Zimbra** :
| Suffixe | Condition | Action suggérée |
|---------|-----------|-----------------|
| ⚠️ | `gmail_status` ou `zimbra_status` = `error` | Vérifier réseau, IMAP, ou logs (`dashboard_update.py` / stderr) |
| 🔑 | `gws_auth_status` = `auth_error` | Reconnecter Google : `gws auth login` (prioritaire sur ⚠️ pour Gmail) |


## Structure

```
dashboard-menubar/
├── menubar.py              # App barre de menus (rumps)
├── dashboard_update.py     # Collecte Calendar + Gmail + Zimbra
├── gws_errors.py           # Détection erreurs OAuth gws
├── zimbra_unread.py        # Accès IMAP à la messagerie Zimbra UL
├── load_env.py             # Charge .env au démarrage
├── .env.example            # Modèle d'identifiants
├── summarize_mail.py       # Résumé du dernier mail (Gmail + Zimbra) via OpenClaw
├── dashboard.json          # Données collectées (gitignored)
├── assets/
│   ├── bell.svg            # Icône source (SVG)
│   └── bell.png            # Icône menubar (PNG 44×44, template)
├── logs/                   # Logs LaunchAgents (gitignored)
├── .gitignore
└── README.md
```

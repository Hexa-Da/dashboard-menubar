# Dashboard Menubar

Une app macOS qui vit dans la barre de menus et affiche en un coup d'oeil :

- **Google Calendar** : prochain événement (titre, horaire, lieu)
- **Gmail** : nombre de mails non lus + expéditeur et résumé IA du dernier mail (via [OpenClaw](https://github.com/nicholasgasior/openclaw))

![macOS](https://img.shields.io/badge/macOS-compatible-blue)
![Python](https://img.shields.io/badge/Python-3.9+-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

## Fonctionnement

```
dashboard_update.py ──(toutes les 10 min)──> dashboard.json
        │                                             │
        └── summarize_mail.py ── OpenClaw ── résumé ──┘
                                                      │
menubar.py ──(toutes les 10 sec)── lit le JSON ───────┘
                                       │
                          barre de menus macOS
```

- **`dashboard_update.py`** appelle les APIs Google Calendar et Gmail via [`gws`](https://github.com/nicholasgasior/gws) et écrit le résultat dans `dashboard.json` à la racine du projet.
- **`summarize_mail.py`** envoie le corps du dernier mail à OpenClaw (`openclaw infer model run`) et écrit un résumé d'une phrase dans le JSON.
- **`menubar.py`** relit ce JSON toutes les 10 secondes et met à jour la barre de menus via [rumps](https://github.com/jaredks/rumps).
- Deux **LaunchAgents** macOS automatisent le tout au démarrage.

## Installation

### Prérequis

- macOS
- Python 3.9+
- [gws](https://github.com/nicholasgasior/gws) configuré avec un compte Google (`gws auth login`)
- [OpenClaw](https://github.com/nicholasgasior/openclaw) pour le résumé IA des mails
- [Homebrew](https://brew.sh/) (recommandé)

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

### LaunchAgents (démarrage automatique)

L'app est conçue pour tourner via deux **LaunchAgents** macOS (`~/Library/LaunchAgents/`) :

- `com.paulantoine.dashboard-update.plist` — exécute `dashboard_update.py` toutes les 10 minutes
- `com.paulantoine.agentmenubar.plist` — maintient `menubar.py` en vie (`KeepAlive`)

```bash
launchctl load ~/Library/LaunchAgents/com.paulantoine.dashboard-update.plist
launchctl load ~/Library/LaunchAgents/com.paulantoine.agentmenubar.plist
```

Les logs sont écrits dans `logs/` à la racine du projet.

## Menu

| Item | Action au clic |
|------|----------------|
| Prochain événement | Ouvre Google Calendar |
| Mails non lus | Ouvre Gmail |
| Dernière mise à jour | Ouvre le fichier JSON |
| Marquer les mails comme lus | Masque le compteur (UI uniquement) |
| Forcer la mise à jour | Appelle les APIs Google et réécrit le JSON |
| Quitter | Ferme l'app |

## Structure

```
dashboard-menubar/
├── menubar.py              # App barre de menus (rumps)
├── dashboard_update.py     # Script de collecte Calendar + Gmail
├── summarize_mail.py       # Résumé du dernier mail via OpenClaw
├── dashboard.json          # Données Calendar + Gmail (gitignored)
├── assets/
│   ├── bell.svg            # Icône source (SVG)
│   └── bell.png            # Icône menubar (PNG 44×44, template)
├── logs/                   # Logs LaunchAgents (gitignored)
├── .gitignore
└── README.md
```

## Licence

MIT

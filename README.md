# Dashboard Menubar

Une app macOS qui vit dans la barre de menus et affiche en un coup d'oeil :

- **Google Calendar** : prochain événement (titre, horaire, lieu)
- **Gmail** : nombre de mails non lus + expéditeur et objet du dernier
- **Ollama** : notification quand un modèle local se charge / se décharge

![macOS](https://img.shields.io/badge/macOS-compatible-blue)
![Python](https://img.shields.io/badge/Python-3.9+-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

## Fonctionnement

```
dashboard_update.py ──(toutes les 10 min)──> agent_dashboard.json
                                                      │
menubar.py ──(toutes les 10 sec)── lit le JSON ───────┘
                                       │
                          barre de menus macOS
```

- **`dashboard_update.py`** appelle les APIs Google Calendar et Gmail via [`gws`](https://github.com/nicholasgasior/gws) et écrit le résultat dans un fichier JSON.
- **`menubar.py`** relit ce JSON toutes les 10 secondes et met à jour la barre de menus via [rumps](https://github.com/jaredks/rumps).
- Deux **LaunchAgents** macOS automatisent le tout au démarrage.

## Installation

### Prérequis

- macOS
- Python 3.9+
- [gws](https://github.com/nicholasgasior/gws) configuré avec un compte Google (`gws auth login`)
- [Homebrew](https://brew.sh/) (recommandé)

### Setup

```bash
# Cloner le repo
git clone https://github.com/<ton-user>/dashboard-menubar.git ~/Documents/dashboard-menubar
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

Copier les deux fichiers plist dans `~/Library/LaunchAgents/` puis les charger :

```bash
# Mise à jour du dashboard toutes les 10 minutes
launchctl load ~/Library/LaunchAgents/com.paulantoine.dashboard-update.plist

# App menubar (KeepAlive)
launchctl load ~/Library/LaunchAgents/com.paulantoine.agentmenubar.plist
```

> Les plists s'attendent à ce que le projet soit dans `~/Documents/dashboard-menubar/`.

## Menu

| Item | Action au clic |
|------|----------------|
| Prochain événement | Ouvre Google Calendar |
| Mails non lus | Ouvre Gmail |
| Rafraîchir la lecture | Relit le JSON immédiatement |
| Marquer les mails comme lus | Masque le compteur (UI uniquement) |
| Forcer la mise à jour | Appelle les APIs Google et réécrit le JSON |
| Quitter | Ferme l'app |

## Structure

```
dashboard-menubar/
├── menubar.py              # App barre de menus (rumps)
├── dashboard_update.py     # Script de collecte Calendar + Gmail
├── assets/
│   ├── bell.svg            # Icône source (SVG)
│   └── bell.png            # Icône menubar (PNG 128x128)
├── .gitignore
└── README.md
```

## Licence

MIT

# Compte rendu : faire fonctionner wayland-mcp sous COSMIC

Fork de [`kurojs/wayland-mcp`](https://github.com/kurojs/wayland-mcp) (GPL-3.0, 7 étoiles,
dernier push amont 2026-05-23), branche `feat/portal-backends`.
Cible : Pop!_OS 24.04, `cosmic-comp 0.1~1788962084~24.04~a557859`, session Wayland.

## Ce qui bloquait exactement

Le diagnostic initial attribuait la panne à `grim` et à l'absence de
`zwlr_screencopy_manager_v1` sous COSMIC. C'est vrai, mais ce n'était ni le premier ni le seul
point de blocage. Dans l'ordre où on les rencontre :

1. **Le paquet ne s'installait pas.** `pyproject.toml` déclarait `urls = {` en table inline
   étalée sur plusieurs lignes, ce que la spec TOML interdit. `uv pip install -e .` échouait sur
   `TOMLDecodeError` avant même d'atteindre hatchling — sur n'importe quelle plateforme. Le wheel
   0.4.0 publié sur PyPI s'installe, lui, d'où l'écart avec `uvx wayland-mcp`.

2. **Le serveur échouait à l'import.** `server_mcp.py` instanciait `MouseController()` et
   `KeyboardController()` au niveau module ; les deux scannaient `/dev/input` et levaient
   `RuntimeError` si aucun device n'était inscriptible — c'est-à-dire sur tout système aux
   permissions saines. `wayland_mcp/__init__.py` important `server_mcp`, même
   `import wayland_mcp.app` plantait.

3. **Aucun backend de capture.** La cascade amont est `ksnip` → `gnome-screenshot` → `grim` →
   `spectacle`. Aucun des quatre n'est présent sous COSMIC, d'où `All capture methods failed`.
   Et `grim` n'aurait pas suffi : la version des dépôts Pop est la 1.4, antérieure à la prise en
   charge de `ext-image-copy-capture`, le seul protocole de capture que `cosmic-comp` expose.

Trois bugs supplémentaires, indépendants de COSMIC, rendaient une partie des outils inopérante
partout :

4. **`execute_action` appelait chaque handler sans argument** (`handler()` contre
   `_handle_type_action(text)`) : `TypeError` non rattrapé sur `type:`, `press:`, `drag:` et
   `scroll:`. Seul un `click` nu fonctionnait.

5. **`execute_action` était annoté `-> bool` en renvoyant des dicts.** Un client MCP conforme
   rejette la réponse contre le schéma déclaré : l'outil était inutilisable depuis un vrai client.

6. **Une chaîne d'actions signalait toujours un succès.** `ChainProcessor._execute_single` rangeait
   le dict retourné par le handler dans son champ `success` ; un dict non vide étant toujours vrai,
   une étape en échec passait pour réussie, ne cassait pas la chaîne, et `all(...)` renvoyait
   `True` quoi qu'il arrive.

Enfin, deux problèmes découverts seulement en exécutant le serveur pour de vrai :

7. **Les clients MCP suppriment l'environnement.** Le transport stdio de référence ne transmet que
   `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` et `USER`. `XDG_RUNTIME_DIR`,
   `DBUS_SESSION_BUS_ADDRESS` et `WAYLAND_DISPLAY` disparaissent, donc tout sondage de capacité
   revient vide : un bureau parfaitement sain est rapporté comme n'ayant ni capture ni saisie.

8. **`cosmic-comp` jette le premier événement synthétisé** après le démarrage d'une session
   RemoteDesktop. Le premier déplacement de pointeur et la première frappe disparaissent, tout ce
   qui suit arrive.

## Sur `setup.sh`

Le script d'installation amont ne demandait pas seulement les droits root : il posait une règle
udev persistante `KERNEL=="event*", GROUP="input", MODE="0666"`, faisait `chmod 666` sur tous les
`/dev/input/event*`, mettait le bit setuid sur `/usr/bin/evemu-event` et ajoutait une règle
sudoers NOPASSWD pour ce même binaire. Tant que la règle udev est en place, **tout processus
local peut lire le clavier en continu**, mots de passe compris. Il n'a jamais été exécuté ici.

Il est retiré du dépôt. `scripts/legacy-evemu-setup.sh` le remplace : il documente ce que faisait
l'original, donne les commandes de rollback exactes, et refuse de s'exécuter.

## Ce qui a été changé

**Sélection par capacité** (`wayland_mcp/backends/`). Un instantané `Capabilities` sonde les
binaires présents et leurs versions, les interfaces de portail sur le bus de session, les globals
annoncés par le compositeur, et l'accès à `/dev/input` et `/dev/uinput`. Chaque backend déclare
ce qu'il exige ; la sélection prend le plus prioritaire dont les exigences sont réellement
satisfaites. Jamais de test sur le nom du compositeur ni sur `XDG_CURRENT_DESKTOP`.

- Capture : `cosmic-screenshot`, `grim` (conditionné à sa version **et** au protocole annoncé),
  `ksnip`/`gnome-screenshot`/`spectacle` comme en amont, portail XDG Screenshot en repli universel.
- Saisie : portail RemoteDesktop d'abord — sans privilège, pointeur et clavier, implémenté par
  GNOME, KDE et COSMIC — puis `wtype`, `ydotool`, et `evemu` en dernier. `evemu` ne s'active que
  si un device est *déjà* inscriptible ; aucun code du fork n'élargit ces permissions.

**Saisie sans privilège et sans dialogue répété.** Le `restore_token` renvoyé par `Start` est
conservé dans `$XDG_STATE_HOME/wayland-mcp/` en mode 0600 et rejoué avec `persist_mode=2` : le
consentement est demandé une fois par machine, pas une fois par démarrage. Un jeton périmé est
réessayé sans lui plutôt que d'être fatal.

**Trois correctifs que seule une exécution réelle pouvait révéler.** La frappe passe par
`NotifyKeyboardKeysym` et non par les keycodes : sur le clavier AZERTY de la machine de
développement, `ASAP 42` arrivait en `QSQP 'é`, parce qu'un keycode désigne une *position*, pas un
caractère. Le défilement passe par `NotifyPointerAxisDiscrete` : `NotifyPointerAxis` transporte
des deltas continus qu'un `Gtk.ScrolledWindow` ignore. Et la session absorbe le premier événement
perdu par un déplacement relatif nul.

**Récupération de l'environnement** (`wayland_mcp/session_env.py`). Les trois variables de session
sont reconstruites depuis le système de fichiers quand elles manquent, jamais écrasées quand elles
sont présentes. Un socket de compositeur est identifié comme `wayland-<chiffres>` avec son `.lock`
jumeau : le socket d'un démon de fond d'écran (`wayland-1-swww-daemon..sock`, observé sur cette
machine) ne rend plus la session ambiguë.

**VLM facultatif.** Le serveur démarre et capture sans aucune clé ; les trois outils d'analyse
disent quelle variable définir. La lecture silencieuse de `~/.roo/mcp.json` devient opt-in
(`WAYLAND_MCP_CONFIG`), et la clé n'est plus journalisée — l'amont en logguait les 15 premiers
caractères.

**Effets de bord supprimés du chemin nominal.** Une capture ne réécrit plus les réglages GNOME de
l'utilisateur, ne coupe plus le son système et n'écrit plus de fichier de thème sonore à l'import.
Ce comportement devient opt-in (`WAYLAND_MCP_QUIET_CAPTURE=1`) et restaure les valeurs réellement
trouvées au lieu de forcer `true`.

**Nouvel outil `describe_environment`** : ce qui est installé, ce que le compositeur annonce, quel
backend a été retenu pour la capture, le clavier et le pointeur, et ce qu'il aurait fallu quand
l'un manque.

**`LICENSE`** contient désormais le texte GPL-3.0 complet. Les 710 octets qu'il remplace ne
contenaient que l'avis court, alors que la licence exige que son texte accompagne l'œuvre.

## Vérification

- **72 tests**, exécutés sans session graphique (`WAYLAND_DISPLAY`, `DISPLAY`, `DBUS_*` et
  `XDG_RUNTIME_DIR` retirés de l'environnement) : sélection de backend pour six profils de
  machine, keysyms et keycodes, récupération d'environnement, chaînes d'actions.
- **Chaque garde-fou a été prouvé falsifiable** par mutation du code de production : priorité
  inversée vers le backend privilégié, contrôle de version de `grim` retiré, contrôle de protocole
  de `wtype` retiré, plage Unicode X11 supprimée, écrasement des variables existantes. Deux tests
  sont restés verts sous mutation et ont été réécrits — ils ne prouvaient rien.
- **Capture réelle sous COSMIC** : PNG 1920×1080, écart-type RGB ≈ 31/28/36 et 11 616 couleurs
  distinctes, donc du contenu d'écran et non un cadre noir.
- **Saisie réelle** (`scripts/verify_input.py`) : une fenêtre GTK relit ce qu'elle a effectivement
  reçu, l'origine de la fenêtre est *mesurée* par calibration du pointeur plutôt que devinée.
  Clic délivré sur le bouton, `Bonjour ASAP 42` tapé exactement sur clavier AZERTY, défilement de
  480 px. Rien n'est déduit de l'absence d'exception.
- **Transport MCP de bout en bout** avec un vrai client stdio : les 10 outils sont exposés, les
  sept formes de dispatch d'`execute_action` renvoient des résultats exploitables, et la capture
  produit une image même quand le client a vidé l'environnement.

## Ce qui reste limité sous COSMIC

- **Capture de région** : `cosmic-screenshot` ne sait sélectionner une région qu'en mode
  interactif, ce qui bloquerait un appel MCP en attendant un humain. Le backend le signale au lieu
  de se figer. Installer `grim` ≥ 1.5 et `slurp` lèverait la limite.
- **Consentement du portail** : une fois par machine, puis silencieux (0,186 s pour une session
  restaurée). Il réapparaît si la permission est révoquée côté bureau.
- **Pointeur absolu** : dépend d'un flux ScreenCast attaché à la session. COSMIC l'accorde, mais
  refuse `persist_mode` sur `ScreenCast.SelectSources` dans une session RemoteDesktop — le passer
  fait échouer tout l'appel avec « Remote desktop sessions cannot persist ». Là où un portail
  refuse le flux, le backend se rabat sur du relatif et le dit.
- **`include_mouse`** n'est honoré par aucun backend disponible ici : `cosmic-screenshot` ne
  propose pas l'option et `grim` ne dessine pas le curseur.
- **Une seule ligne de la matrice a été vérifiée sur matériel** : COSMIC. Les lignes GNOME, KDE et
  wlroots découlent des exigences déclarées de chaque backend et des tests qui figent la sélection,
  pas d'une exécution réelle.

## Pull request amont : envisageable

Le diff est additif. Aucun backend existant n'est retiré, les signatures publiques de
`MouseController` et `KeyboardController` sont préservées, la cascade historique
`ksnip`/`gnome-screenshot`/`spectacle` reste en place aux mêmes priorités relatives, et les
chemins `grim` et `evemu` continuent de fonctionner là où ils fonctionnaient. Les six premiers
points du diagnostic sont des bugs purs qu'un mainteneur devrait vouloir, indépendamment de
COSMIC — le TOML invalide et le crash à l'import empêchent toute installation depuis les sources.

Deux points demanderont une discussion plutôt qu'une simple revue : la suppression de `setup.sh`
(un mainteneur peut préférer le conserver derrière un avertissement) et le passage du VLM en
option, qui change le comportement par défaut. Les découper en commits séparés — ce qui est déjà
le cas — permet de les proposer indépendamment.

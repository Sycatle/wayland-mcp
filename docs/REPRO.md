# Reproduction de la panne amont (avant correctifs)

Machine : Pop!_OS 24.04, `cosmic-comp 0.1~1788962084~24.04~a557859`, session Wayland
(`WAYLAND_DISPLAY=wayland-1`), Python 3.12.3. Binaires absents : `grim`, `slurp`, `wtype`,
`ydotool`, `evemu-tools`, `ksnip`, `gnome-screenshot`, `spectacle`. Présents :
`cosmic-screenshot`, `cosmic-randr`, `ffmpeg`.

`setup.sh` n'a **pas** été exécuté (voir README, section sécurité).

## Blocage 0 — le paquet ne s'installe pas

    $ uv pip install -e .
    tomllib.TOMLDecodeError: Invalid initial character for a key part (at line 35, column 10)

`pyproject.toml` déclarait `urls = {` en table inline étalée sur plusieurs lignes, ce que la
spec TOML interdit. L'amont à HEAD est non installable depuis les sources sur toute plateforme.
Le wheel 0.4.0 publié sur PyPI, lui, s'installe — d'où l'écart avec `uvx wayland-mcp`.

## Blocage 1 — le serveur échoue à l'import

    $ wayland-mcp
    File "wayland_mcp/server_mcp.py", line 69, in <module>
        mouse = MouseController()
    File "wayland_mcp/mouse_utils.py", line 75, in _auto_detect_device
        raise RuntimeError(error_msg)
    RuntimeError: No suitable mouse device found. Check permissions and devices in /dev/input/

`server_mcp.py` instanciait `MouseController()` et `KeyboardController()` au niveau module.
Aucun `/dev/input/event*` n'est inscriptible (comportement par défaut correct d'un système sain),
donc le serveur meurt avant d'exposer le moindre outil. `wayland_mcp/__init__.py` important
`server_mcp`, même `import wayland_mcp.app` plantait.

## Blocage 2 — aucun backend de capture

En chargeant `app.py` directement pour contourner le blocage 1 :

    RESULT: {'success': False, 'error': 'All capture methods failed'}

La cascade amont est `ksnip` → `gnome-screenshot` → `grim` → `spectacle`. Aucun n'est présent.
`grim` n'aurait de toute façon pas suffi : la version des dépôts Pop est la 1.4, qui ne parle que
`zwlr_screencopy_manager_v1`, absent de `cosmic-comp`. COSMIC n'expose que le plus récent
`ext_image_copy_capture_manager_v1`.

## Effet de bord observé

Le seul appel à `capture_screenshot` a créé `~/.local/share/sounds/silent/stereo/screen-capture.oga`,
appelé `gsettings set` sur les réglages GNOME de l'utilisateur et coupé/rétabli le son système
via `pactl`.

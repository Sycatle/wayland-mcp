"""Input via org.freedesktop.portal.RemoteDesktop.

This is the answer to upstream's root requirement. RemoteDesktop synthesises
pointer and keyboard events with no privilege at all, and GNOME, KDE and COSMIC
all implement it -- unlike zwp_virtual_keyboard (absent on GNOME) or ydotool
(needs /dev/uinput).

One wrinkle shapes the code below: NotifyPointerMotionAbsolute takes a *stream*,
which only exists if a ScreenCast session is attached to the same handle. So the
session is opened as RemoteDesktop + ScreenCast when absolute positioning is
wanted, and degrades to relative-only motion when ScreenCast is unavailable or
refused.
"""
import logging

from wayland_mcp.backends.base import (
    IFACE_REMOTE_DESKTOP,
    IFACE_SCREENCAST,
    Capabilities,
    InputBackend,
)
from wayland_mcp.backends import portal
from wayland_mcp.backends.keycodes import evdev_keycode, parse_combo

#: Linux input button codes, what NotifyPointerButton expects.
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}

#: RemoteDesktop device bitmask: 1 = keyboard, 2 = pointer, 4 = touchscreen.
DEVICE_KEYBOARD = 1
DEVICE_POINTER = 2

#: Session persistence: 0 none, 1 while the app runs, 2 until revoked.
PERSIST_UNTIL_REVOKED = 2

#: One wheel notch, in the units NotifyPointerAxis uses.
AXIS_STEP = 120.0


class PortalRemoteDesktopBackend(InputBackend):
    """A persistent RemoteDesktop session, started on first use."""

    name = "portal"
    priority = 80
    can_pointer = True
    can_keyboard = True
    absolute_pointer = True
    requires = (
        "a running XDG desktop portal exposing org.freedesktop.portal.RemoteDesktop, "
        "plus PyGObject (install the 'portal' extra, or python3-gi)"
    )

    def __init__(self):
        self._session = None
        self._handle = None
        self._stream = None
        self._screen = None
        self._restore_token = None
        self._can_screencast = True

    def supports(self, caps: Capabilities) -> bool:
        self._can_screencast = IFACE_SCREENCAST in caps.dbus_interfaces
        return IFACE_REMOTE_DESKTOP in caps.dbus_interfaces and caps.has_gi

    # -- session lifecycle -------------------------------------------------

    def _ensure_session(self):
        """Open the session once; every later call reuses it."""
        if self._handle is not None:
            return
        from gi.repository import GLib  # pylint: disable=import-outside-toplevel

        portal.log_permission_hint("Pointer and keyboard control")
        self._session = portal.PortalSession()

        create_token = portal.token()
        results = self._session.request(
            IFACE_REMOTE_DESKTOP,
            "CreateSession",
            GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", create_token),
                "session_handle_token": GLib.Variant("s", portal.token()),
            },)),
            create_token,
        )
        self._handle = results["session_handle"]

        select_token = portal.token()
        select_options = {
            "handle_token": GLib.Variant("s", select_token),
            "types": GLib.Variant("u", DEVICE_KEYBOARD | DEVICE_POINTER),
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }
        if self._restore_token:
            select_options["restore_token"] = GLib.Variant("s", self._restore_token)
        self._session.request(
            IFACE_REMOTE_DESKTOP,
            "SelectDevices",
            GLib.Variant("(oa{sv})", (self._handle, select_options)),
            select_token,
        )

        if self._can_screencast:
            self._select_screencast_sources()

        start_token = portal.token()
        started = self._session.request(
            IFACE_REMOTE_DESKTOP,
            "Start",
            GLib.Variant("(osa{sv})", (
                self._handle, "", {"handle_token": GLib.Variant("s", start_token)},
            )),
            start_token,
            # The consent dialog is a human in the loop.
            timeout_ms=180000,
        )
        self._restore_token = started.get("restore_token")
        self._adopt_streams(started.get("streams") or [])
        logging.info(
            "RemoteDesktop session started (absolute pointer: %s)",
            "yes" if self._stream is not None else "no, relative only",
        )

    def _select_screencast_sources(self):
        """Attach a ScreenCast source, needed for absolute pointer coordinates."""
        from gi.repository import GLib  # pylint: disable=import-outside-toplevel

        sources_token = portal.token()
        try:
            self._session.request(
                IFACE_SCREENCAST,
                "SelectSources",
                GLib.Variant("(oa{sv})", (self._handle, {
                    "handle_token": GLib.Variant("s", sources_token),
                    "types": GLib.Variant("u", 1),  # monitors
                    "multiple": GLib.Variant("b", False),
                    "cursor_mode": GLib.Variant("u", 2),  # embedded, so captures show it
                    "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
                })),
                sources_token,
            )
        except portal.PortalError as err:
            # Not fatal: we lose absolute positioning, not the whole session.
            logging.warning("ScreenCast source selection failed (%s); pointer will be "
                            "relative only", err)
            self._can_screencast = False

    def _adopt_streams(self, streams):
        """Remember the first stream's node id and size for absolute motion."""
        for node_id, props in streams:
            self._stream = node_id
            size = props.get("size")
            if size:
                self._screen = (int(size[0]), int(size[1]))
            return

    def close(self):
        if self._handle and self._session:
            try:
                self._session.call_on(
                    self._handle, "org.freedesktop.portal.Session", "Close", None, None
                )
            except portal.PortalError as err:
                logging.debug("Closing RemoteDesktop session failed: %s", err)
        self._handle = None
        self._stream = None

    # -- pointer -----------------------------------------------------------

    @property
    def screen_size(self):
        """Size of the captured stream, or None when unknown."""
        return self._screen

    def move_pointer(self, x, y, relative=False) -> bool:
        from gi.repository import GLib  # pylint: disable=import-outside-toplevel

        self._ensure_session()
        if relative or self._stream is None:
            if not relative:
                raise portal.PortalError(
                    "absolute pointer positioning needs a ScreenCast stream, which this "
                    "portal did not grant; move relatively or use another input backend"
                )
            self._notify(
                "NotifyPointerMotion",
                GLib.Variant("(oa{sv}dd)", (self._handle, {}, float(x), float(y))),
            )
            return True
        self._notify(
            "NotifyPointerMotionAbsolute",
            GLib.Variant("(oa{sv}udd)", (
                self._handle, {}, self._stream, float(x), float(y),
            )),
        )
        return True

    def click(self, button="left", press=True, release=True) -> bool:
        from gi.repository import GLib  # pylint: disable=import-outside-toplevel

        self._ensure_session()
        code = BUTTONS.get(button)
        if code is None:
            raise ValueError(f"unknown mouse button {button!r}")
        for state in ([1] if press else []) + ([0] if release else []):
            self._notify(
                "NotifyPointerButton",
                GLib.Variant("(oa{sv}iu)", (self._handle, {}, code, state)),
            )
        return True

    def scroll(self, amount, horizontal=False) -> bool:
        from gi.repository import GLib  # pylint: disable=import-outside-toplevel

        self._ensure_session()
        # Positive means up/left for us, as upstream; the portal's axis grows
        # downwards, hence the negation.
        delta = -float(amount) * AXIS_STEP
        dx, dy = (delta, 0.0) if horizontal else (0.0, delta)
        self._notify(
            "NotifyPointerAxis",
            GLib.Variant("(oa{sv}dd)", (self._handle, {}, dx, dy)),
        )
        return True

    # -- keyboard ----------------------------------------------------------

    def type_text(self, text) -> bool:
        self._ensure_session()
        for char in text:
            keycode, shift = evdev_keycode(char)
            if keycode is None:
                logging.warning("No keycode for %r; skipped", char)
                continue
            self._tap([evdev_keycode("shift")[0]] if shift else [], keycode)
        return True

    def press_key(self, key) -> bool:
        self._ensure_session()
        modifiers, main = parse_combo(key)
        self._tap(modifiers, main)
        return True

    def _tap(self, modifiers, keycode):
        """Press modifiers, tap the key, release modifiers in reverse order."""
        for modifier in modifiers:
            self._key(modifier, 1)
        try:
            self._key(keycode, 1)
            self._key(keycode, 0)
        finally:
            for modifier in reversed(modifiers):
                self._key(modifier, 0)

    def _key(self, keycode, state):
        from gi.repository import GLib  # pylint: disable=import-outside-toplevel

        self._notify(
            "NotifyKeyboardKeycode",
            GLib.Variant("(oa{sv}iu)", (self._handle, {}, int(keycode), state)),
        )

    def _notify(self, method, args):
        self._session.call(IFACE_REMOTE_DESKTOP, method, args, None)

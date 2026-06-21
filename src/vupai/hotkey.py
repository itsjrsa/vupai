from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from pynput import keyboard

_log = logging.getLogger(__name__)

# Curated push-to-talk keys offered by the setup menu: (pynput Key name, label).
# Modifier keys make good held PTT keys; F13-F19 are common dedicated keys.
# Every entry must be a real keyboard.Key name (see valid_key) or the daemon's
# listener crashes at spawn. On macOS pynput collapses the left modifiers
# (alt_l->alt, cmd_l->cmd, ctrl_l->ctrl) so capture_key reports those bare
# names; the list uses them so the menu, capture, and config all agree.
PTT_KEYS: list[tuple[str, str]] = [
    ("alt_r", "Right-Option"),
    ("alt", "Left-Option"),
    ("cmd_r", "Right-Command"),
    ("cmd", "Left-Command"),
    ("ctrl_r", "Right-Control"),
    ("ctrl", "Left-Control"),
    ("f13", "F13"),
    ("f14", "F14"),
    ("f15", "F15"),
    ("f16", "F16"),
    ("f17", "F17"),
    ("f18", "F18"),
    ("f19", "F19"),
]


def valid_key(name: str) -> bool:
    """True if `name` resolves to a pynput keyboard.Key.

    This is the same lookup Hotkey/MultiHotkey do at construction, so it accepts
    exactly the names that won't crash the listener.
    """
    return getattr(keyboard.Key, name, None) is not None


def capture_key(
    *,
    listener_factory: Callable[[Callable], Any] | None = None,
    timeout: float = 5.0,
) -> str | None:
    """Block until the user presses one named key, returning its config name.

    Listens for the first keyboard.Key press (a modifier/function key, not a
    character) and returns its `.name` (e.g. "alt_r"). Character keys are
    skipped (no `.name`); returns None on timeout. `listener_factory` is
    injectable for tests so no real pynput listener runs.
    """
    def _default_factory(on_press):
        return keyboard.Listener(on_press=on_press)

    factory = listener_factory if listener_factory is not None else _default_factory

    captured: dict[str, str] = {}
    done = threading.Event()

    def on_press(key) -> bool | None:
        name = getattr(key, "name", None)
        if name is None:
            return None  # a character KeyCode; keep waiting for a named key
        captured["name"] = name
        done.set()
        return False  # stop the listener

    listener = factory(on_press)
    listener.start()
    done.wait(timeout)
    try:
        listener.stop()
    except Exception:
        _log.debug("listener.stop() raised", exc_info=True)
    return captured.get("name")


class Hotkey:
    """Push-to-talk hotkey with auto-repeat debounce.

    pynput's on_press fires repeatedly while a key is held; the _held flag
    ensures on_press/on_release each fire once per physical press cycle.

    Exceptions from the user callbacks are caught and logged so that the
    pynput listener thread is never killed by application errors.
    """

    def __init__(self, key_name: str, on_press: Callable[[], None],
                 on_release: Callable[[], None]) -> None:
        # Map the configured key name string to a pynput Key (e.g. "alt_r").
        self._key = getattr(keyboard.Key, key_name)
        self._on_press = on_press
        self._on_release = on_release
        self._held: bool = False
        self._listener: keyboard.Listener | None = None

    def _press(self, key) -> None:
        if key != self._key:
            return
        if self._held:
            return  # ignore auto-repeat while held
        self._held = True
        try:
            self._on_press()
        except Exception:
            _log.exception("on_press callback raised")

    def _release(self, key) -> None:
        if key != self._key:
            return
        if not self._held:
            return
        self._held = False
        try:
            self._on_release()
        except Exception:
            _log.exception("on_release callback raised")

    def start(self) -> None:
        # Listener runs on its own background thread (non-blocking).
        self._listener = keyboard.Listener(
            on_press=self._press,
            on_release=self._release,
        )
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


class MultiHotkey:
    """Push-to-talk over several keys, each independently debounced.

    One pynput Listener feeds every bound key. Each binding maps a pynput Key to
    its (on_press, on_release) callbacks plus a per-key held flag, so holding one
    key never disturbs another's debounce. Callback exceptions are caught and
    logged so the listener thread is never killed by application errors.
    """

    def __init__(
        self,
        bindings: list[tuple[str, Callable[[], None], Callable[[], None]]],
    ) -> None:
        self._on_press: dict = {}
        self._on_release: dict = {}
        self._held: dict = {}
        for key_name, on_press, on_release in bindings:
            key = getattr(keyboard.Key, key_name)
            self._on_press[key] = on_press
            self._on_release[key] = on_release
            self._held[key] = False
        self._listener: keyboard.Listener | None = None

    def _press(self, key) -> None:
        cb = self._on_press.get(key)
        if cb is None or self._held[key]:
            return  # unbound key, or auto-repeat while held
        self._held[key] = True
        try:
            cb()
        except Exception:
            _log.exception("on_press callback raised")

    def _release(self, key) -> None:
        cb = self._on_release.get(key)
        if cb is None or not self._held[key]:
            return
        self._held[key] = False
        try:
            cb()
        except Exception:
            _log.exception("on_release callback raised")

    def start(self) -> None:
        self._listener = keyboard.Listener(
            on_press=self._press,
            on_release=self._release,
        )
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

from __future__ import annotations

import logging
from typing import Callable

from pynput import keyboard

_log = logging.getLogger(__name__)


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

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

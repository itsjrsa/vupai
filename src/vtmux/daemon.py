"""Daemon orchestrator: hotkey -> record -> transcribe -> route -> inject -> feedback.

v1 scope: targets Claude Code panes only. Injecting into other agent CLIs
(Codex/OpenCode) is out of scope for now due to known send-keys submit bugs.
"""
from __future__ import annotations

import threading
from pathlib import Path

from .asr import Transcriber
from .config import Config
from .feedback import Feedback
from .hotkey import Hotkey
from .injector import inject
from .recorder import MIN_WAV_BYTES, Recorder
from .registry import PaneRegistry
from .router import Route, route


class Daemon:
    """Wires hotkey -> record -> transcribe -> route -> inject -> feedback."""

    def __init__(self, config: Config, recorder: Recorder, transcriber: Transcriber,
                 registry: PaneRegistry, feedback: Feedback,
                 *, route_fn=route, inject_fn=inject) -> None:
        self._config = config
        self._recorder = recorder
        self._transcriber = transcriber
        self._registry = registry
        self._feedback = feedback
        self._route_fn = route_fn
        self._inject_fn = inject_fn
        self._hotkey: Hotkey | None = None
        self._stop_event = threading.Event()
        self._mic_hint_shown = False

    def on_press(self) -> None:
        self._recorder.start()
        self._feedback.status("listening...")

    def on_release(self) -> None:
        wav: Path = self._recorder.stop()

        # Guard against an empty capture (mic permission / device issue).
        try:
            size = wav.stat().st_size
        except OSError:
            size = 0
        if size < MIN_WAV_BYTES:
            if not self._mic_hint_shown:
                self._feedback.error(
                    "no audio captured - grant Microphone access in "
                    "System Settings > Privacy & Security > Microphone")
                self._mic_hint_shown = True
            else:
                self._feedback.error("no audio captured")
            return

        self._registry.refresh()
        hints = [p.name for p in self._registry.panes if p.name != p.id]
        text = self._transcriber.transcribe(wav, hints=hints)
        if not text or not text.strip():
            self._feedback.status("didn't catch that")
            return

        focused = self._registry.focused()
        focused_id = focused.id if focused is not None else None
        route_obj = self._route_fn(
            text, self._registry.panes, focused_id,
            fuzzy_cutoff=self._config.fuzzy_cutoff)

        if route_obj.pane_id is None:
            self._feedback.error("no target")
            return

        ok = self._inject_fn(
            route_obj.pane_id, route_obj.text,
            confirm_timeout=self._config.inject_confirm_timeout,
            poll_interval=self._config.inject_poll_interval)
        if ok:
            self._feedback.announce(route_obj)
            return

        # Injection failed: the routed pane may have gone away. Re-resolve the
        # registry and fall back to the focused pane once before giving up.
        self._registry.refresh()
        focused = self._registry.focused()
        if focused is not None and focused.id != route_obj.pane_id:
            retry = Route(pane_id=focused.id, text=route_obj.text,
                          matched_name=None, confidence=0.0, fallback=True)
            if self._inject_fn(
                    retry.pane_id, retry.text,
                    confirm_timeout=self._config.inject_confirm_timeout,
                    poll_interval=self._config.inject_poll_interval):
                self._feedback.announce(retry)
                return
        self._feedback.error("injection failed - text not confirmed in pane")

    def run(self) -> None:
        self._transcriber.warm()
        self._hotkey = Hotkey(self._config.hotkey, self.on_press, self.on_release)
        self._hotkey.start()
        self._feedback.status("ready")
        # Block forever; on_press/on_release fire from the listener thread.
        self._stop_event.wait()

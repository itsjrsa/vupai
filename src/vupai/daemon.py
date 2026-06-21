"""Daemon orchestrator: hotkey -> record -> transcribe -> route -> inject -> feedback.

v1 scope: targets Claude Code panes only. Injecting into other agent CLIs
(Codex/OpenCode) is out of scope for now due to known send-keys submit bugs.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from .asr import Transcriber
from .commands import handle_command
from .config import Config
from .feedback import Feedback
from .hotkey import Hotkey, MultiHotkey
from .injector import inject
from .journal import Journal
from .recorder import MIN_WAV_BYTES, Recorder
from .registry import PaneRegistry
from .router import Route, route

logger = logging.getLogger(__name__)

# Sentinel enqueued by stop() to unblock the consumer loop for a clean shutdown.
_SHUTDOWN = object()


def _spawn_thread(fn, *args) -> None:
    """Default off-thread dispatcher for listener-thread feedback (see _async)."""
    threading.Thread(target=fn, args=args, daemon=True).start()


class Daemon:
    """Wires hotkey -> record -> transcribe -> route -> inject -> feedback."""

    def __init__(self, config: Config, recorder: Recorder, transcriber: Transcriber,
                 registry: PaneRegistry, feedback: Feedback,
                 *, route_fn=route, inject_fn=inject, command_fn=handle_command,
                 journal: Journal | None = None, async_fn=None) -> None:
        self._config = config
        self._recorder = recorder
        self._transcriber = transcriber
        self._registry = registry
        self._feedback = feedback
        self._route_fn = route_fn
        self._inject_fn = inject_fn
        self._command_fn = command_fn
        # How off-listener-thread feedback is dispatched; tests inject a
        # synchronous runner so listener-thread indicator calls are deterministic.
        self._async_fn = async_fn if async_fn is not None else _spawn_thread
        self._journal = journal if journal is not None else Journal.from_config(config)
        self._hotkey: Hotkey | MultiHotkey | None = None
        self._stop_event = threading.Event()
        self._mic_hint_shown = False
        self._active_mode = "keyword"   # mode the in-flight recording was started under
        # Push-to-talk is serial, but the listener thread must never block on the
        # heavy pipeline (a slow pynput tap callback gets disabled by macOS), so
        # on_release hands the wav to this queue and the main-thread consumer in
        # run() does transcribe/route/inject. Bounded so a stuck consumer can't
        # grow memory without bound; a full queue drops the utterance with a warn.
        self._jobs: queue.Queue = queue.Queue(maxsize=8)

    def _on_press(self, mode: str) -> None:
        if self._recorder.is_recording:
            return  # another key already holds the mic; ignore
        self._active_mode = mode
        self._recorder.start()
        # Listener-thread hot path: the status-line indicator touches tmux, which
        # must not run on the pynput callback (invariant), so offload it. Reserve
        # the ordering seq NOW (press time) so a slow write can't clobber a newer
        # working/result state painted while it was still in flight.
        self._async(self._feedback.listening, mode, self._feedback.reserve())

    def _on_release(self, mode: str) -> None:
        # Only the key that started the capture stops it.
        if not self._recorder.is_recording or self._active_mode != mode:
            return
        # Listener-thread hot path: stop sox and hand (wav, mode) to the main-
        # thread consumer. MUST stay cheap - no MLX, no tmux, no inject here.
        try:
            wav: Path = self._recorder.stop()
        except RuntimeError:
            return  # release without a matching start (debounce edge); ignore
        except Exception:
            # Any other stop() failure (a wedged sox kill, OS error): the wav is
            # unusable. Never let it escape into pynput's wrapper - that would
            # swallow it AND leave the 'listening' indicator (painted at press)
            # stuck on. Log it and repaint an error so the state clears.
            logger.exception("recorder stop failed on release")
            self._async(self._feedback.error, "recording failed - try again",
                        self._feedback.reserve())
            return
        try:
            self._jobs.put_nowait((wav, mode))
        except queue.Full:
            self._async(self._feedback.error,
                        "busy - dropped (still processing previous)",
                        self._feedback.reserve())

    def _async(self, fn, *args) -> None:
        """Run a best-effort feedback call off the listener thread. The status-
        line write spawns a tmux subprocess; keeping it off the pynput callback
        avoids a slow tap (which macOS disables) and honours the no-tmux-on-the-
        listener-thread invariant."""
        self._async_fn(fn, *args)

    def on_press(self) -> None:
        self._on_press("keyword")

    def on_release(self) -> None:
        self._on_release("keyword")

    def _process(self, wav: Path, mode: str = "keyword") -> None:
        """Full pipeline for one utterance. Runs on the same OS thread as warm()
        (the main thread) so MLX's thread-local GPU stream matches. Synchronous
        and side-effect-only, so unit tests can drive it directly.

        Every exit path is journaled once (transcript + decision + outcome) via
        the `entry`/`finally` below, so misfires can be reviewed after the fact.
        """
        entry: dict = {
            "v": 1,
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "mode": mode,
            "transcript": "",
            "decision": None,
            "outcome": None,
            "model_id": self._config.model_id,
        }
        keep_wav = False
        try:
            # Guard against an empty capture (mic permission / device issue).
            try:
                size = wav.stat().st_size
            except OSError:
                size = 0
            if size < MIN_WAV_BYTES:
                entry["decision"] = "no_audio"
                entry["outcome"] = "no_audio"
                if not self._mic_hint_shown:
                    self._feedback.error(
                        "no audio captured - grant Microphone access in "
                        "System Settings > Privacy & Security > Microphone")
                    self._mic_hint_shown = True
                else:
                    self._feedback.error("no audio captured")
                return

            keep_wav = True  # a real capture: worth retaining if audio is on
            self._feedback.working()  # transcribe+route can take a couple seconds
            self._registry.refresh()
            hints = [p.name for p in self._registry.panes if p.name != p.id]
            _t0 = time.monotonic()
            text = self._transcriber.transcribe(wav, hints=hints)
            entry["transcribe_ms"] = round((time.monotonic() - _t0) * 1000)
            entry["transcript"] = text
            if not text or not text.strip():
                entry["decision"] = "empty"
                entry["outcome"] = "no_transcript"
                self._feedback.status("didn't catch that")
                return

            if mode == "dictation":
                entry["decision"] = "dictation"
                ok = self._inject_dictation(text)
                entry["outcome"] = "injected" if ok else "inject_failed"
                return

            # Command layer: utterances addressed to the control/broadcast word
            # are interpreted by vupai itself, not injected into a pane.
            result = self._command_fn(
                text, self._registry, self._config, inject_fn=self._inject_fn,
                addressing="button" if mode == "system" else "keyword")
            if result is not None:
                entry["decision"] = "command"
                entry["command"] = result.message
                entry["outcome"] = "ok" if result.ok else "unknown"
                (self._feedback.status if result.ok else self._feedback.error)(result.message)
                return

            entry["decision"] = "route"
            focused = self._registry.focused()
            focused_id = focused.id if focused is not None else None
            route_obj = self._route_fn(
                text, self._registry.panes, focused_id,
                fuzzy_cutoff=self._config.fuzzy_cutoff)
            entry["confidence"] = route_obj.confidence
            entry["match_method"] = route_obj.match_method
            entry["available_names"] = list(hints)

            if route_obj.candidates:
                # Ambiguous near-tie: don't guess. Surface candidates and bail.
                entry["outcome"] = "ambiguous"
                entry["candidates"] = list(route_obj.candidates)
                self._feedback.error(
                    "ambiguous: " + " / ".join(route_obj.candidates)
                    + " - say the name again")
                return

            if route_obj.pane_id is None:
                entry["outcome"] = "no_target"
                self._feedback.error("no target")
                return

            entry["target_pane"] = route_obj.pane_id
            entry["target_name"] = route_obj.matched_name
            entry["fallback"] = route_obj.fallback
            _i0 = time.monotonic()
            ok = self._inject_fn(
                route_obj.pane_id, route_obj.text,
                confirm_timeout=self._config.inject_confirm_timeout,
                poll_interval=self._config.inject_poll_interval)
            entry["inject_ms"] = round((time.monotonic() - _i0) * 1000)
            if ok:
                entry["outcome"] = "injected"
                self._feedback.announce(route_obj)
                return

            # Injection failed: the routed pane may have gone away. Re-resolve
            # the registry and fall back to the focused pane once before giving up.
            self._registry.refresh()
            focused = self._registry.focused()
            if focused is not None and focused.id != route_obj.pane_id:
                retry = Route(pane_id=focused.id, text=route_obj.text,
                              matched_name=None, confidence=0.0, fallback=True)
                _i1 = time.monotonic()
                if self._inject_fn(
                        retry.pane_id, retry.text,
                        confirm_timeout=self._config.inject_confirm_timeout,
                        poll_interval=self._config.inject_poll_interval):
                    entry["inject_ms"] = round((time.monotonic() - _i1) * 1000)
                    entry["outcome"] = "injected_fallback"
                    entry["target_pane"] = retry.pane_id
                    self._feedback.announce(retry)
                    return
            entry["outcome"] = "inject_failed"
            self._feedback.error("injection failed - text not confirmed in pane")
        finally:
            self._journal.record(entry, wav if keep_wav else None)
            # The recorder owns the temp wav's creation; the daemon owns its
            # deletion. Journal.record has already COPIED it if retention is on,
            # so unlink the source unconditionally - otherwise every utterance
            # leaks a wav into $TMPDIR for the daemon's whole lifetime.
            try:
                wav.unlink(missing_ok=True)
            except OSError:
                pass

    def _inject_dictation(self, text: str) -> bool:
        """Verbatim injection into the focused pane: no command parse, no name
        routing. The literal-text guarantee of the dictation key. Returns True
        when the paste was confirmed."""
        focused = self._registry.focused()
        if focused is None:
            self._feedback.error("no focused pane")
            return False
        ok = self._inject_fn(
            focused.id, text,
            confirm_timeout=self._config.inject_confirm_timeout,
            poll_interval=self._config.inject_poll_interval)
        if ok:
            self._feedback.announce(Route(
                pane_id=focused.id, text=text, matched_name=None,
                confidence=0.0, fallback=True))
        else:
            self._feedback.error("injection failed - text not confirmed in pane")
        return ok

    def _make_hotkey(self):
        """Pick the listener for the configured addressing mode. Button mode
        needs two distinct, valid keys; on any misconfiguration fall back to a
        single keyword Hotkey so the daemon still works as push-to-talk."""
        if self._config.addressing == "button":
            dict_key = self._config.hotkey
            sys_key = self._config.command_hotkey
            if dict_key == sys_key:
                self._feedback.error(
                    "addressing=button needs distinct hotkey/command_hotkey - "
                    "falling back to keyword mode")
            else:
                try:
                    return MultiHotkey([
                        (dict_key, lambda: self._on_press("dictation"),
                         lambda: self._on_release("dictation")),
                        (sys_key, lambda: self._on_press("system"),
                         lambda: self._on_release("system")),
                    ])
                except AttributeError:
                    self._feedback.error(
                        f"unknown key name in config (hotkey={dict_key!r}, "
                        f"command_hotkey={sys_key!r}) - falling back to keyword mode")
        return Hotkey(self._config.hotkey, self.on_press, self.on_release)

    def run(self) -> None:
        # warm() establishes MLX's thread-local GPU stream on THIS (main) thread;
        # every _process -> transcribe below runs on the same thread, so the
        # stream always matches. Heavy work is kept off the listener thread (see
        # on_release), so the consumer loop lives here on the warm thread.
        self._transcriber.warm()
        self._hotkey = self._make_hotkey()
        self._hotkey.start()
        self._feedback.ready()
        try:
            while not self._stop_event.is_set():
                job = self._jobs.get()  # blocks until an utterance or the sentinel
                if job is _SHUTDOWN:
                    break
                try:
                    self._process(*job)
                except Exception:
                    # One bad utterance must never kill the daemon loop.
                    logger.exception("utterance processing failed")
                    try:
                        self._feedback.error("internal error - see daemon log")
                    except Exception:
                        pass
        finally:
            self._hotkey.stop()
            # A clean shutdown (SIGTERM via `vupai down`) can land while PTT is
            # held. Reap the in-flight recorder so the sox child isn't orphaned
            # holding the mic for the next daemon.
            if self._recorder.is_recording:
                try:
                    self._recorder.stop()
                except Exception:
                    logger.exception("recorder cleanup on shutdown failed")

    def stop(self) -> None:
        """Request a clean shutdown of the consumer loop (e.g. from a signal)."""
        self._stop_event.set()
        try:
            self._jobs.put_nowait(_SHUTDOWN)
        except queue.Full:
            # Make room so the consumer observes the sentinel promptly.
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                pass
            try:
                self._jobs.put_nowait(_SHUTDOWN)
            except queue.Full:
                pass

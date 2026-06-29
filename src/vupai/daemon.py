"""Daemon orchestrator: hotkey -> record -> transcribe -> route -> inject -> feedback.

v1 scope: targets Claude Code panes only. Injecting into other agent CLIs
(Codex/OpenCode) is out of scope for now due to known send-keys submit bugs.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import speech
from .asr import Transcriber, model_cached
from .commands import (
    DESTRUCTIVE_KINDS,
    Command,
    execute_command,
    intent_phrase,
    parse_command,
)
from .config import Config
from .confirm import DEFAULT_DISABLE_HINT, popup_confirm
from .feedback import Feedback
from .filler import strip_fillers
from .hotkey import Hotkey, MultiHotkey
from .injector import inject
from .journal import Journal
from .recorder import MIN_WAV_BYTES, Recorder, reap_orphan_recordings
from .registry import PaneRegistry
from .router import Route, _peel_fillers, match_leading_names, route

logger = logging.getLogger(__name__)

# Sentinel enqueued by stop() to unblock the consumer loop for a clean shutdown.
_SHUTDOWN = object()

# Talk-back is curated, not blanket: speak what you can't see, stay quiet when the
# screen already shows it, always speak failures.
#
# _ANNOUNCE_INTENT - kinds that voice an immediate present-tense intent on issue.
# These act on things off-screen or are irreversible (a new agent's callsign you
# may miss, a kill, a fan-out to agents you're not looking at), so a spoken ack
# carries information. The view/navigation verbs (focus / zoom / unzoom / layout /
# swap) are deliberately ABSENT: the cursor jump / resize / re-tile is its own
# instant feedback, so speaking it is redundant and naggy in rapid use. They still
# speak on FAILURE (every kind does, in _run_command) - that is the eyes-off case
# you cannot see. read / talkback are handled on their own paths, not here.
_ANNOUNCE_INTENT = frozenset(
    {"create", "close", "close_others", "broadcast", "slash", "board", "ssh"})

# Subset of _ANNOUNCE_INTENT whose SUCCESS also voices the result, because it
# carries information the intent could not (a create's assigned callsign; a
# talkback toggle's confirmation; a volume nudge's new level, spoken at that
# level so you hear it). Every other announced kind is intent-only on success -
# the present-tense ack already said it.
_SPEAK_ON_SUCCESS = frozenset({"create", "talkback", "volume", "ssh"})

# Shown on every empty capture. Covers BOTH causes (a denied Microphone grant
# AND a disconnected/muted/name-collided device) and is emitted unconditionally -
# a mid-session unplug is the common case, so the message must not blame
# permission once and then go quiet.
_NO_AUDIO_MSG = (
    "no audio captured - check the mic is connected and unmuted, or grant "
    "Microphone access in System Settings > Privacy & Security > Microphone"
)


def _spawn_thread(fn, *args) -> None:
    """Default off-thread dispatcher for listener-thread feedback (see _async)."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def _summarize_destructive(cmd: Command, registry) -> str:
    """Short description of a confirmation-gated command for the prompt/journal."""
    if cmd.kind == "close":
        return f"close {', '.join(cmd.names) if cmd.names else cmd.name}"
    if cmd.kind == "close_others":
        focused = registry.focused()
        others = [p for p in registry.panes
                  if focused is None or p.id != focused.id]
        return f"close {len(others)} other pane(s)"
    if cmd.kind == "broadcast":
        if cmd.names:
            return f"broadcast to {', '.join(cmd.names)}: {cmd.text[:30]}"
        return f"broadcast to all agents: {cmd.text[:30]}"
    if cmd.kind == "create":
        return f"open {cmd.count} panes"
    return cmd.kind


def _disable_hint(cmd: Command) -> str:
    """Config hint shown in the popup's '(disable: ...)' footer, targeted to the
    command: a large create points at its own threshold, so turning this one
    popup off doesn't mean disabling all destructive confirmations."""
    if cmd.kind == "create":
        return "raise confirm_create_threshold in config.toml"
    return DEFAULT_DISABLE_HINT


class Daemon:
    """Wires hotkey -> record -> transcribe -> route -> inject -> feedback."""

    def __init__(self, config: Config, recorder: Recorder, transcriber: Transcriber,
                 registry: PaneRegistry, feedback: Feedback,
                 *, hosts=None, route_fn=route, inject_fn=inject,
                 parse_fn=parse_command, execute_fn=execute_command,
                 confirm_fn=popup_confirm,
                 journal: Journal | None = None, async_fn=None,
                 state_writer=None, watcher=None, tip_rotator=None, activity=None,
                 read_registry_factory=None) -> None:
        self._config = config
        self._hosts = hosts or {}
        self._recorder = recorder
        self._transcriber = transcriber
        self._registry = registry
        self._feedback = feedback
        self._route_fn = route_fn
        self._inject_fn = inject_fn
        # Command interpretation (parse_fn) is kept separate from execution
        # (execute_fn) so the destructive-confirmation gate can inspect the
        # parsed Command's kind BEFORE acting on it.
        self._parse_fn = parse_fn
        self._execute_fn = execute_fn
        # Destructive commands are gated by a synchronous confirmation (default:
        # a tmux popup). confirm_fn(summary, *, timeout) -> bool; injected for tests.
        self._confirm_fn = confirm_fn
        # Optional agent-state poller (watcher.PaneWatcher) - runs on its own
        # thread, touches only tmux + osascript, never this pipeline. None = off.
        self._watcher = watcher
        # Optional rotating status-bar tips (tips.TipRotator) - own thread,
        # touches only tmux, never this pipeline. None = off.
        self._tip_rotator = tip_rotator
        self._activity = activity  # Optional cross-pane activity ledger poller
        # (activity.ActivityPoller) - own thread, touches only tmux + read-only
        # git + its .vupai store, never this pipeline. None = off.
        # Optional lifecycle marker writer: state_writer("ready") after warm(),
        # state_writer("stopped") on a clean exit. The marker's absence after a
        # dead pid is how `vupai status` distinguishes a crash from a clean stop.
        self._state_writer = state_writer
        # How off-listener-thread feedback is dispatched; tests inject a
        # synchronous runner so listener-thread indicator calls are deterministic.
        self._async_fn = async_fn if async_fn is not None else _spawn_thread
        # The spoken "read" command runs on a worker thread (see _dispatch_read)
        # with its OWN registry, never self._registry - the main loop refreshes
        # that on every utterance and sharing it would race the refresh. A factory
        # (not an instance) so each read sees a fresh, independently-refreshed view.
        self._read_registry_factory = read_registry_factory or PaneRegistry
        # Runtime master switch for ALL spoken talk-back (command acks + read).
        # Seeded from config.tts_enabled (the persisted default) and flipped live
        # by the "mute"/"unmute" voice command. A plain bool: written on the main
        # thread, read on the read worker - atomic enough in CPython, no lock.
        self._talkback = config.tts_enabled
        # Runtime readback volume, 0.0..1.0. Seeded from config.tts_volume (the
        # persisted default) and nudged live by the "louder"/"quieter" voice
        # command, exactly mirroring _talkback vs config.tts_enabled. Read by
        # _speak on every utterance; a plain float, same no-lock rationale as
        # _talkback (written on the main thread, read on the read worker).
        self._tts_volume = min(1.0, max(0.0, config.tts_volume))
        # Barge-in plumbing. _read_cancel is the active streaming read's cancel
        # Event (set on barge-in -> the SentenceSpeaker drains silently and the
        # summarizer subprocess is killed). _last_ack is the most recent spoken
        # process handle (an ack or a read sentence, since both go through
        # _speak); terminating it cuts the currently-playing `say` mid-sentence.
        # No lock: pointer read/write is CPython-atomic, best-effort semantics,
        # consistent with _talkback above. Accepted consequence: after a barge-in,
        # at most one already-queued sentence may still finish (cancel stops the
        # worker before the NEXT sentence; _last_ack cuts the current one, but a
        # race can leave a just-started sentence to proc.wait() out).
        self._read_cancel: "threading.Event | None" = None
        self._last_ack = None
        self._journal = journal if journal is not None else Journal.from_config(config)
        self._hotkey: Hotkey | MultiHotkey | None = None
        self._stop_event = threading.Event()
        self._active_mode = ""   # mode the in-flight recording was started under
        # Push-to-talk is serial, but the listener thread must never block on the
        # heavy pipeline (a slow pynput tap callback gets disabled by macOS), so
        # on_release hands the wav to this queue and the main-thread consumer in
        # run() does transcribe/route/inject. Bounded so a stuck consumer can't
        # grow memory without bound; a full queue drops the utterance with a warn.
        self._jobs: queue.Queue = queue.Queue(maxsize=8)

    def _on_press(self, mode: str) -> None:
        # Barge-in: the instant the user presses to speak, stop talking. Besides
        # the natural feel, it stops `say` audio bleeding into the mic and
        # polluting the transcript. Cheap (Event.set + terminate); safe here.
        self._silence()
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
        # Convenience entry for the system (command) key; the listener binds the
        # dictation/system keys to _on_press directly with their modes.
        self._on_press("system")

    def on_release(self) -> None:
        self._on_release("system")

    def _process(self, wav: Path, mode: str = "system") -> None:
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
                self._feedback.error(_NO_AUDIO_MSG)
                return

            keep_wav = True  # a real capture: worth retaining if audio is on
            self._feedback.working()  # transcribe+route can take a couple seconds
            self._registry.refresh()
            pane_names = [p.name for p in self._registry.panes if p.name != p.id]
            # Bias the ASR toward pane names AND program tokens (codex/opencode/
            # pi): "codex" otherwise mishears as "codecs", "opencode" as "open
            # code". The command-layer aliases recover these, but biasing the
            # acoustic model first means fewer mishearings to recover from.
            hints = pane_names + [p for p in self._config.programs if p]
            _t0 = time.monotonic()
            text = self._transcriber.transcribe(wav, hints=hints)
            entry["transcribe_ms"] = round((time.monotonic() - _t0) * 1000)
            entry["transcript"] = text
            if self._config.filler_filter:
                cleaned = strip_fillers(text, self._config.filler_words)
                if cleaned != text:
                    entry["filtered_transcript"] = cleaned
                text = cleaned
            if not text or not text.strip():
                entry["decision"] = "empty"
                entry["outcome"] = "no_transcript"
                self._feedback.status("didn't catch that")
                return

            # HUD: echo what was heard on the focused pane so a mishearing or
            # misroute is visible. Verbatim dictation is skipped (it lands in the
            # pane as the literal text anyway).
            if mode != "dictation":
                self._feedback.heard(text, self._hud_pane(), mode=mode)

            if mode == "dictation":
                entry["decision"] = "dictation"
                result = self._inject_dictation(text)
                if result is True:
                    entry["outcome"] = "injected"
                elif result is None:
                    entry["outcome"] = "cancelled"
                else:
                    entry["outcome"] = "inject_failed"
                return

            # Command layer: utterances addressed to the control/broadcast word
            # are interpreted by vupai itself, not injected into a pane.
            cmd = self._parse_fn(
                text, broadcast_word=self._config.broadcast_word,
                macros=self._config.macros, programs=self._config.programs,
                slash_commands=self._config.slash_commands)
            if cmd is not None:
                if cmd.kind == "read":
                    # Read is slow (LLM summary) and audible; it runs off the main
                    # thread so it never stalls the next utterance. See _dispatch_read.
                    self._dispatch_read(cmd, entry)
                    return
                if cmd.kind == "activity":
                    # Slow (git + capture) and audible: run off the main thread.
                    self._dispatch_read(cmd, entry)
                    return
                if cmd.kind == "talkback":
                    # Flip the runtime mute BEFORE running so _run_command's spoken
                    # ack honors the new state: unmute confirms aloud, mute stays
                    # silent (it's already muted). No tmux mutation, no confirm gate.
                    self._talkback = cmd.enable
                    self._run_command(cmd, entry)
                    return
                if cmd.kind == "volume":
                    # Adjust the live level BEFORE running so the success ack (in
                    # _SPEAK_ON_SUCCESS) is spoken at the new volume. Stamp the
                    # clamped level onto the command so the executor reports the
                    # exact value applied. No tmux mutation, no confirm gate.
                    self._tts_volume = min(
                        1.0, max(0.0, self._tts_volume + cmd.volume_delta))
                    self._run_command(
                        replace(cmd, volume_level=self._tts_volume), entry)
                    return
                if cmd.kind == "stop":
                    self._handle_stop(cmd, entry)
                    return
                self._dispatch_command(cmd, entry)
                return

            # Subset broadcast: a leading run of 2+ named panes + a message
            # ("echo and sage, run tests") fans the message to just those panes.
            # Registry-aware (the parser is pure), so it lives here. Runs in both
            # modes, like single-name addressing; confirm-gated as a broadcast.
            names, message = match_leading_names(
                text, self._registry.panes, fuzzy_cutoff=self._config.fuzzy_cutoff)
            if len(names) < 2:
                peeled, npeel = _peel_fillers(text)
                if npeel:
                    names, message = match_leading_names(
                        peeled, self._registry.panes,
                        fuzzy_cutoff=self._config.fuzzy_cutoff)
            if len(names) >= 2 and message.strip():
                entry["decision"] = "command"
                self._dispatch_command(
                    Command(kind="broadcast", names=tuple(names), text=message), entry)
                return

            entry["decision"] = "route"
            focused = self._registry.focused()
            focused_id = focused.id if focused is not None else None
            route_obj = self._route_fn(
                text, self._registry.panes, focused_id,
                fuzzy_cutoff=self._config.fuzzy_cutoff)
            entry["confidence"] = route_obj.confidence
            entry["match_method"] = route_obj.match_method
            entry["available_names"] = list(pane_names)

            if route_obj.candidates:
                # Ambiguous near-tie: don't guess. Surface candidates and bail.
                entry["outcome"] = "ambiguous"
                entry["candidates"] = list(route_obj.candidates)
                self._feedback.reject(
                    "ambiguous - say the name again", self._hud_pane(),
                    candidates=tuple(route_obj.candidates))
                return

            # System (button command) key: accept only commands (handled above)
            # or an utterance that addresses a pane BY NAME/NUMBER. An unaddressed
            # utterance lands on the focus fallback - verbatim-to-focused is the
            # dictation key's job, so swallow it here instead of typing it into the
            # focused pane (the misfire that made the system key "accept everything").
            if mode == "system" and route_obj.fallback:
                entry["outcome"] = "not_addressed"
                self._feedback.reject(
                    "not a command - name a pane or use the dictation key",
                    self._hud_pane())
                return

            if route_obj.pane_id is None:
                entry["outcome"] = "no_target"
                self._feedback.reject("no target", self._hud_pane())
                return

            entry["target_pane"] = route_obj.pane_id
            entry["target_name"] = route_obj.matched_name
            entry["fallback"] = route_obj.fallback
            _i0 = time.monotonic()
            ok = self._inject_fn(
                route_obj.pane_id, route_obj.text,
                confirm_timeout=self._config.inject_confirm_timeout,
                poll_interval=self._config.inject_poll_interval,
                **self._submit_delay_kw())
            entry["inject_ms"] = round((time.monotonic() - _i0) * 1000)
            if ok is True:
                entry["outcome"] = "injected"
                self._feedback.announce(route_obj)
                return
            if ok is None:  # user cleared the input during the review window
                entry["outcome"] = "cancelled"
                self._feedback.status("cancelled - input cleared before send")
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
                        poll_interval=self._config.inject_poll_interval,
                        **self._submit_delay_kw()) is True:
                    entry["inject_ms"] = round((time.monotonic() - _i1) * 1000)
                    entry["outcome"] = "injected_fallback"
                    entry["target_pane"] = retry.pane_id
                    self._feedback.announce(retry)
                    return
            entry["outcome"] = "inject_failed"
            self._feedback.reject(
                "injection failed - text not confirmed in pane",
                route_obj.pane_id or self._hud_pane())
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

    def _needs_confirm(self, cmd: Command) -> bool:
        """True if `cmd` must pass the confirmation popup. Destructive kinds
        always qualify; a create qualifies once its count reaches
        confirm_create_threshold (a large fan-out tiles tight and degrades voice
        addressing). Both share the confirm_destructive master switch upstream."""
        if cmd.kind in DESTRUCTIVE_KINDS:
            return True
        if cmd.kind == "create":
            return cmd.count >= self._config.confirm_create_threshold
        return False

    def _dispatch_read(self, cmd: Command, entry: dict) -> None:
        """Speak a pane's summary off the main thread.

        Read is the one command that is BOTH slow (an LLM summary) and audible
        (`say` blocks until the phrase ends), so running it inline would stall
        transcription of the next utterance. The worker (_run_read) gets its own
        registry to stay clear of the main loop's refresh. The journal records the
        dispatch here, synchronously, since the worker's outcome lands after this
        utterance's entry is already written; the spoken result is surfaced (and
        logged via feedback) from the worker.
        """
        entry["decision"] = "command"
        entry["command"] = "read " + (cmd.name or "(focused)")
        entry["outcome"] = "dispatched"
        cancel = threading.Event()
        self._read_cancel = cancel
        self._async_fn(self._run_read, cmd, cancel)

    def _run_read(self, cmd: Command, cancel: "threading.Event | None" = None) -> None:
        """Worker body for a read: resolve + summarize + speak via execute_fn, on a
        fresh registry, then surface the spoken line.

        Runs on a background thread, so it has two hard rules: never raise (a daemon
        thread dies silently, swallowing the outcome) and never touch self._registry
        (the main loop owns it and refreshes it on every utterance) - both would
        break the isolation that is the whole point of running off-thread.
        """
        registry = self._read_registry_factory()
        try:
            registry.refresh()
        except Exception:
            logger.debug("read registry refresh failed", exc_info=True)
            return
        try:
            result = self._execute_fn(
                cmd, registry, self._config, inject_fn=self._inject_fn,
                speak_fn=self._speak, cancel=cancel)
            if result.ok:
                self._feedback.status(result.message)
            else:
                # HUD target from the worker's OWN (just-refreshed) registry, never
                # self._hud_pane() - that reads self._registry and would race the
                # main loop's refresh.
                focused = registry.focused()
                self._feedback.reject(
                    result.message, focused.id if focused is not None else None)
        except Exception:
            logger.debug("read worker failed", exc_info=True)
        finally:
            # Clear _read_cancel so a later _silence() after this read completes
            # is a clean no-op. Identity check: a concurrent _dispatch_read may have
            # already stored a fresh Event B; only clear when it's still OUR Event.
            if cancel is not None and self._read_cancel is cancel:
                self._read_cancel = None

    def _dispatch_command(self, cmd: Command, entry: dict) -> None:
        """Voice the intent, apply the confirm gate, then run `cmd`.

        Shared by the parsed-command path and the synthesized subset-broadcast
        path so both get the same immediate intent ack and confirmation popup.
        Mirrors the previous inline block exactly; the read/talkback/stop kinds
        are handled by their own early-returns before this is ever reached.
        """
        if cmd.kind in _ANNOUNCE_INTENT:
            self._speak(intent_phrase(cmd))
        if self._config.confirm_destructive and self._needs_confirm(cmd):
            summary = _summarize_destructive(cmd, self._registry)
            if not self._confirm_fn(
                    summary, timeout=self._config.confirm_timeout_s,
                    disable_hint=_disable_hint(cmd)):
                entry["decision"] = "command"
                entry["command"] = summary
                entry["outcome"] = "cancelled"
                self._feedback.status(f"cancelled: {summary}")
                self._speak("cancelled")
                return
            self._run_command(cmd, entry, confirmed=True)
            return
        self._run_command(cmd, entry)

    def _run_command(self, cmd: Command, entry: dict, *, confirmed: bool = False):
        """Execute a parsed command and record the result. `confirmed` marks a
        destructive command that went through the confirmation gate."""
        result = self._execute_fn(
            cmd, self._registry, self._config,
            inject_fn=self._inject_fn, hosts=self._hosts)
        entry["decision"] = "command"
        entry["command"] = result.message
        if confirmed:
            entry["confirmed"] = True
        entry["outcome"] = "ok" if result.ok else "unknown"
        if result.ok:
            self._feedback.status(result.message)
            # Success is normally silent - the immediate intent ack (spoken before
            # execute) already covered it. Only kinds whose result adds new info
            # (a create's callsign, a talkback toggle) speak on success.
            if cmd.kind in _SPEAK_ON_SUCCESS:
                # A create voiced "opening an agent" a split-second ago; _speak
                # serializes, so this "<name> is up" result waits for that intent
                # to finish instead of running together with it.
                self._speak(result.spoken or result.message)
        else:
            self._feedback.reject(result.message, self._hud_pane())
            # Failure always speaks: the intent said "closing sage", so the user
            # needs to hear it didn't happen ("no pane named sage").
            self._speak(result.spoken or result.message)
        return result

    def _speak(self, text: str):
        """Speak `text` via the configured TTS, gated by the runtime mute switch.

        Serialized: each utterance waits on the previous one's handle before
        spawning, so back-to-back acks (a create's "opening an agent" intent then
        its "sage is up" result) never talk over each other. The wait is bounded
        (a `say` phrase is seconds) and interruptible - a barge-in (_silence)
        terminates the in-flight handle, so the wait returns at once and the main
        thread is never wedged. Shared by command acks and the read worker (every
        streamed sentence flows through here), so the "mute"/"unmute" toggle covers
        both AND _silence can terminate whatever is currently playing via the stored
        handle. Returns the process handle (or None when muted/failed) so the read
        worker's SentenceSpeaker can wait on it too."""
        if not self._talkback or not self._config.tts_cmd:
            return None
        prev = self._last_ack
        if prev is not None:
            try:
                prev.wait()  # let the previous utterance finish; no overlap
            except Exception:
                pass
        try:
            # Full volume (>=1.0) is the unchanged path: pass no `volume` so the
            # phrase is byte-identical to before. Below full, the level rides as a
            # `say` [[volm]] directive (other backends ignore it - see speech.speak).
            vol = self._tts_volume
            handle = (speech.speak(text, cmd=self._config.tts_cmd) if vol >= 1.0
                      else speech.speak(text, cmd=self._config.tts_cmd, volume=vol))
        except Exception:
            logger.debug("talk-back speak failed", exc_info=True)
            return None
        self._last_ack = handle
        return handle

    def _silence(self) -> None:
        """Cut all in-flight talk-back: cancel the streaming read and terminate
        the currently-playing utterance. Cheap and listener-thread-safe (only an
        Event.set and a Popen.terminate), so it is safe to call from _on_press.
        Best-effort: a missing or already-finished handle is a no-op. Does NOT
        touch the persistent _talkback switch - this is transient."""
        ev = self._read_cancel
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass
        handle = self._last_ack
        if handle is not None:
            try:
                handle.terminate()
            except Exception:
                pass

    def _handle_stop(self, cmd: Command, entry: dict) -> None:
        """Transient barge-in word: silence in-flight talk-back, mute nothing.

        The press that carried this utterance already silenced (see _on_press);
        this also covers the case where speech started between press and parse,
        and records the outcome. No tmux mutation, no confirm gate, no speech."""
        self._silence()
        entry["decision"] = "command"
        entry["command"] = "stop"
        entry["outcome"] = "ok"
        self._feedback.status("stopped")

    def _hud_pane(self) -> str | None:
        """The pane to show HUD overlays on: the focused pane, or None."""
        focused = self._registry.focused()
        return focused.id if focused is not None else None

    def _submit_delay_kw(self) -> dict:
        """Pass the configured review delay to the injector, but only when set -
        so the many bool-returning inject_fn fakes/callers are untouched at 0.0."""
        delay = self._config.inject_submit_delay
        return {"submit_delay": delay} if delay else {}

    def _inject_dictation(self, text: str):
        """Verbatim injection into the focused pane: no command parse, no name
        routing. The literal-text guarantee of the dictation key. Returns True
        when the paste was confirmed and submitted, None if cancelled in the
        review window, False if the paste was never confirmed."""
        focused = self._registry.focused()
        if focused is None:
            self._feedback.reject("no focused pane", None)
            return False
        ok = self._inject_fn(
            focused.id, text,
            confirm_timeout=self._config.inject_confirm_timeout,
            poll_interval=self._config.inject_poll_interval,
            **self._submit_delay_kw())
        if ok is True:
            self._feedback.announce(Route(
                pane_id=focused.id, text=text, matched_name=None,
                confidence=0.0, fallback=True))
        elif ok is None:
            self._feedback.status("cancelled - input cleared before send")
        else:
            self._feedback.reject(
                "injection failed - text not confirmed in pane", focused.id)
        return ok

    def _make_hotkey(self):
        """Build the two-key push-to-talk listener.

        Both hotkey fields are tuples of pynput key names (see config); any key
        in a list triggers that action. Every dictation key is bound to the
        dictation callbacks and every system key to the command callbacks on one
        MultiHotkey. On any misconfiguration (overlapping keys, an empty list, or
        an unknown key name) it falls back to the default keys so the daemon
        still works as push-to-talk."""
        def build(dict_keys, sys_keys):
            bindings = [
                (k, lambda: self._on_press("dictation"),
                 lambda: self._on_release("dictation"))
                for k in dict_keys
            ] + [
                (k, lambda: self._on_press("system"),
                 lambda: self._on_release("system"))
                for k in sys_keys
            ]
            return MultiHotkey(bindings)

        dict_keys = self._config.hotkey or ("alt_r",)
        sys_keys = self._config.command_hotkey or ("cmd_r",)
        if set(dict_keys) & set(sys_keys):
            self._feedback.error(
                "hotkey/command_hotkey must not overlap - using default keys")
            dict_keys, sys_keys = ("alt_r",), ("cmd_r",)
        try:
            return build(dict_keys, sys_keys)
        except AttributeError:
            self._feedback.error(
                f"unknown key name in config (hotkey={dict_keys!r}, "
                f"command_hotkey={sys_keys!r}) - using default keys")
            return build(("alt_r",), ("cmd_r",))

    def run(self) -> None:
        # warm() establishes MLX's thread-local GPU stream on THIS (main) thread;
        # every _process -> transcribe below runs on the same thread, so the
        # stream always matches. Heavy work is kept off the listener thread (see
        # on_release), so the consumer loop lives here on the warm thread.
        # A previous daemon that died hard (kill -9, crash) can leave its `rec`
        # child orphaned to launchd, holding the mic open. We haven't spawned any
        # rec yet, so anything matching vupai's signature now is stale - reap it.
        try:
            reaped = reap_orphan_recordings()
            if reaped:
                logger.warning("reaped %d orphaned rec process(es) at startup", reaped)
        except Exception:
            logger.exception("orphan reap at startup failed")
        # Paint a warming state BEFORE the (potentially multi-minute first-run)
        # model load, so a cold start doesn't look like a dead hotkey.
        self._feedback.warming(downloading=not model_cached(self._config.model_id))
        self._transcriber.warm()
        if self._state_writer is not None:
            self._state_writer("ready")  # model loaded; hotkey about to go live
        self._hotkey = self._make_hotkey()
        self._hotkey.start()
        self._feedback.ready()
        if self._watcher is not None:
            self._watcher.start()  # background agent-state poller (own thread)
        if self._tip_rotator is not None:
            self._tip_rotator.start()  # rotating status-bar tips (own thread)
        if self._activity is not None:
            self._activity.start()  # background cross-pane activity poller
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
            # Quiesce the activity poll thread first so it can't capture-pane /
            # git mid-teardown.
            if self._activity is not None:
                try:
                    self._activity.stop()
                except Exception:
                    logger.exception("activity poller stop on shutdown failed")
            if self._tip_rotator is not None:
                try:
                    self._tip_rotator.stop()
                except Exception:
                    logger.exception("tip rotator stop on shutdown failed")
            # Quiesce the watcher poll thread before the hotkey so it can't
            # capture-pane mid-teardown (the activity poller is stopped above).
            if self._watcher is not None:
                try:
                    self._watcher.stop()
                except Exception:
                    logger.exception("watcher stop on shutdown failed")
            self._hotkey.stop()
            # A clean shutdown (SIGTERM via `vupai down`) can land while PTT is
            # held. Reap the in-flight recorder so the sox child isn't orphaned
            # holding the mic for the next daemon.
            if self._recorder.is_recording:
                try:
                    self._recorder.stop()
                except Exception:
                    logger.exception("recorder cleanup on shutdown failed")
            # Clean-exit marker, written LAST. A dead pid without it == a crash.
            if self._state_writer is not None:
                try:
                    self._state_writer("stopped")
                except Exception:
                    logger.exception("writing stopped state marker failed")

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

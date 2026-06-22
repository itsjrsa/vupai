import threading

from vupai import tmuxio
from vupai.router import Route

# Per-state style for the tmux status-line indicator: (tmux style, glyph). The
# `#[fg=...]` codes are interpreted by tmux at draw time (see install_status_
# indicator). Kept small and ambient - one glyph + a short label.
_INDICATOR_STYLES = {
    "idle": ("#[fg=green]", "●"),
    "listening": ("#[fg=red]", "◉"),
    "busy": ("#[fg=cyan]", "…"),
    "warming": ("#[fg=yellow]", "◌"),
    "ok": ("#[fg=green]", "▸"),
    "info": ("#[default]", "·"),
    "warn": ("#[fg=yellow]", "·"),
    "error": ("#[fg=red]", "⚠"),
}
_INDICATOR_MAX = 36  # truncate labels so the status segment stays compact
_HUD_MAX = 60        # truncate the heard-transcript pane overlay


class Feedback:
    """User-facing feedback on three channels:

    - the tmux **status line** (always visible to the attached user): an ambient
      indicator of daemon state - listening / working / last result / errors -
      since the daemon is detached and its stdout only reaches the log;
    - a transient **announcement** on the routed target pane (`display-message`);
    - stdout, which lands in the daemon log for after-the-fact diagnosis.
    """

    def __init__(self, io=tmuxio, *, indicator_enabled: bool = True,
                 hud_enabled: bool = True) -> None:
        self._io = io
        self._indicator_enabled = indicator_enabled
        self._hud_enabled = hud_enabled
        # Monotonic ordering for indicator writes. The "listening" write happens
        # on a background thread (off the pynput listener), so a quick tap can let
        # it land *after* the main thread already wrote working/result and clobber
        # it (stale "listening" sticks). Each write reserves a seq at the logical
        # moment via `reserve()`; a write whose seq is older than the last applied
        # one is dropped, so newer state always wins regardless of thread timing.
        self._seq = 0
        self._applied = 0
        self._lock = threading.Lock()

    def reserve(self) -> int:
        """Reserve a monotonic sequence at the moment an event occurs. Pass it to
        a deferred indicator write so ordering reflects real time, not run time."""
        with self._lock:
            self._seq += 1
            return self._seq

    def indicator(self, label: str, kind: str = "info", seq: int | None = None) -> None:
        """Set the tmux status-line indicator. Best-effort: the status line must
        never break the voice pipeline, so every failure (no tmux, no client, an
        io fake without set_status) is swallowed. `seq` (from `reserve()`) orders
        deferred writes; omit it for an immediate, in-order write."""
        if not self._indicator_enabled:
            return
        if seq is None:
            seq = self.reserve()
        with self._lock:
            if seq < self._applied:
                return  # a newer state already won; don't clobber it
            self._applied = seq
        style, glyph = _INDICATOR_STYLES.get(kind, _INDICATOR_STYLES["info"])
        label = label[:_INDICATOR_MAX]
        try:
            self._io.set_status(f"{style}{glyph} {label}#[default]")
        except Exception:
            pass

    def warming(self, downloading: bool = False) -> None:
        # Painted before the (blocking) model load so a cold start doesn't look
        # dead. `downloading` marks the multi-minute first-run fetch vs a quick
        # in-cache load. Printed too, so a headless start is visible in the log.
        if downloading:
            label, line = "downloading model", "warming (downloading model ~600MB)..."
        else:
            label, line = "warming", "warming (loading model)..."
        print(line)
        self.indicator(label, "warming")

    def ready(self) -> None:
        print("ready")
        self.indicator("vupai", "idle")

    def listening(self, mode: str = "keyword", seq: int | None = None) -> None:
        # Shown while the push-to-talk key is held and the mic is open. Written
        # off the listener thread, so it carries a reserved seq (see indicator).
        if mode == "system":
            print("listening (system)...")
            self.indicator("listening ·sys", "listening", seq)
        else:
            print("listening...")
            self.indicator("listening", "listening", seq)

    def working(self) -> None:
        # Transcribe -> route -> inject can take a couple of seconds (the model).
        self.indicator("working", "busy")

    def status(self, text: str) -> None:
        # Plain status line printed to the daemon log; mirrored to the indicator.
        print(text)
        self.indicator(text, "info")

    def _pane_msg(self, pane_id: str | None, label: str) -> None:
        """Best-effort transient overlay on a pane (tmux display-message).

        Gated by hud_enabled and a present pane_id; every failure is swallowed so
        the HUD can never break the voice pipeline. Shared by announce/heard/reject.
        """
        if not self._hud_enabled or not pane_id:
            return
        try:
            self._io.display_message(pane_id, label)
        except Exception:
            pass

    def heard(self, transcript: str, pane_id: str | None,
              *, mode: str = "keyword") -> None:
        # Echo what was heard on the target pane so a mishearing/misroute is
        # visible where the user is looking. Informational: no error indicator.
        self._pane_msg(pane_id, f"heard: {transcript[:_HUD_MAX]}")

    def announce(self, route: Route) -> None:
        # Only announce when we actually routed somewhere.
        if route.pane_id is None:
            return
        snippet = route.text[:40]
        if route.matched_name:
            label = f"◀ {route.matched_name}: {snippet}"
        else:
            label = f"◀ (focus): {snippet}"
        self._pane_msg(route.pane_id, label)
        self.indicator(route.matched_name or "focus", "ok")

    def reject(self, reason: str, pane_id: str | None,
               *, candidates: tuple[str, ...] = ()) -> None:
        # A rejected utterance: surface the reason on BOTH the status indicator
        # (always) and the target pane (HUD). candidates list the near-ties.
        label = reason
        if candidates:
            label = f"{reason}: " + " / ".join(candidates)
        print(f"reject: {label}")
        self.indicator(reason, "error")
        self._pane_msg(pane_id, f"⚠ {label}")

    def error(self, text: str, seq: int | None = None) -> None:
        # Error lines are prefixed so they stand out in the daemon log. `seq` is
        # supplied when called off the listener thread (the busy-dropped path).
        print(f"error: {text}")
        self.indicator(text, "error", seq)

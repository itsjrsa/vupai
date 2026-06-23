"""Thin, exact wrappers over the tmux CLI.

Every helper builds the precise argv tmux expects and delegates execution to
``run``. ``run`` raises :class:`TmuxError` on a nonzero exit, surfacing stderr.
"""

from __future__ import annotations

import os
import subprocess

# Field 5 is the voice name, stored in the per-pane user option @vupai_name
# (NOT pane_title): the target apps - Claude Code in particular - overwrite
# pane_title with their own string, but never touch @ user options. The option
# is empty when unset; registry.parse_panes falls back to the pane id there.
PANE_FORMAT = "\t".join(
    [
        "#{pane_id}",
        "#{window_id}",
        "#{window_name}",
        "#{pane_index}",
        "#{@vupai_name}",
        "#{pane_current_command}",
        "#{pane_active}",
        "#{session_name}",
    ]
)


class TmuxError(RuntimeError):
    """Raised when a tmux command exits nonzero."""


def _base_argv() -> list[str]:
    # Tests may pin an isolated server via a private socket name.
    socket = os.environ.get("VTMUX_TMUX_SOCKET")
    if socket:
        return ["tmux", "-L", socket]
    return ["tmux"]


def run(args: list[str], *, stdin: str | None = None) -> str:
    """Run ``tmux <args>``; return stdout. Raise TmuxError on nonzero exit."""
    proc = subprocess.run(
        _base_argv() + args,
        input=stdin,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise TmuxError(proc.stderr.strip() or f"tmux {' '.join(args)} failed")
    return proc.stdout


def list_panes() -> list[str]:
    out = run(["list-panes", "-a", "-F", PANE_FORMAT])
    return [line for line in out.splitlines() if line.strip()]


def focused_pane_id() -> str | None:
    """Pane id of the pane the user is most likely looking at.

    The daemon runs detached, with no controlling tmux client, so a bare
    `display-message -p '#{pane_id}'` resolves against the server's globally
    most-recently-active session. On a multi-session server (one session per
    repo) that can be a *different* repo's session, so a voice command would
    split panes in the wrong session/cwd. Anchor instead to the most recently
    active *attached* client and evaluate the active pane in that client's
    context, so focus follows the terminal the user last used. Fall back to the
    bare query when no client is attached.
    """
    client = _latest_client()
    args = ["display-message", "-p", "#{pane_id}"]
    if client is not None:
        args[1:1] = ["-c", client]
    try:
        out = run(args)
    except TmuxError:
        return None
    return out.strip() or None


def _latest_client() -> str | None:
    """Name of the most recently active attached client, or None if none."""
    try:
        out = run(["list-clients", "-F", "#{client_activity}\t#{client_name}"])
    except TmuxError:
        return None
    best_name: str | None = None
    best_activity = -1
    for line in out.splitlines():
        if "\t" not in line:
            continue
        activity, _, name = line.partition("\t")
        if not name:
            continue
        try:
            ts = int(activity)
        except ValueError:
            ts = 0
        if ts >= best_activity:
            best_activity, best_name = ts, name
    return best_name


def load_buffer(text: str) -> None:
    run(["load-buffer", "-"], stdin=text)


def paste_buffer(pane_id: str) -> None:
    run(["paste-buffer", "-p", "-d", "-t", pane_id])


def capture_pane(pane_id: str) -> str:
    # -J joins tmux-wrapped lines so a long pasted line that wraps at the
    # terminal width is captured as one contiguous line (the injector's
    # confirmation needle would otherwise straddle a wrap break and never match).
    return run(["capture-pane", "-J", "-p", "-t", pane_id])


def send_enter(pane_id: str) -> None:
    run(["send-keys", "-t", pane_id, "Enter"])


def set_pane_name(pane_id: str, name: str) -> None:
    # Store the voice name in a per-pane user option the target app can't clobber
    # (unlike pane_title). Read back via @vupai_name in PANE_FORMAT.
    run(["set", "-p", "-t", pane_id, "@vupai_name", name])


def set_pane_program(pane_id: str, label: str) -> None:
    # Store the launched program (e.g. "claude") in a per-pane user option, like
    # @vupai_name. pane_title can't carry this: agents overwrite it with their
    # own summary ("✳ Add help command..."), which erases the program identity.
    # Read back via @vupai_program in the pane-border-format. Empty label is fine
    # (plain-shell panes); the format's #{?...} hides the segment when unset.
    run(["set", "-p", "-t", pane_id, "@vupai_program", label])


def pane_program(pane_id: str) -> str:
    """Read back a pane's @vupai_program label (the program vupai launched).

    Empty when unset (plain shell, or a pane vupai did not create). Used by the
    board to pick per-tool state markers; `-q` keeps an unset option from raising.
    """
    try:
        return run(["show", "-pqv", "-t", pane_id, "@vupai_program"]).strip()
    except TmuxError:
        return ""


def mark_board_pane(pane_id: str) -> None:
    """Tag a pane as the supervision board so it can be excluded from watching."""
    run(["set", "-p", "-t", pane_id, "@vupai_board", "1"])


def pane_session(pane_id: str) -> str:
    """Session name owning `pane_id` (empty string when it can't be resolved)."""
    try:
        return run(["display-message", "-p", "-t", pane_id, "#{session_name}"]).strip()
    except TmuxError:
        return ""


def find_board_pane(session: str) -> str | None:
    """Pane id of an existing supervision board in `session`, else None.

    Used to keep `vupai board` from opening a second board in a session (two
    boards would summarize each other's frames). Reads the @vupai_board tag set
    by mark_board_pane.
    """
    fmt = "\t".join(["#{@vupai_board}", "#{session_name}", "#{pane_id}"])
    try:
        out = run(["list-panes", "-a", "-F", fmt])
    except TmuxError:
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "1" and parts[1] == session:
            return parts[2]
    return None


def enable_pane_titles() -> None:
    run(["set", "-g", "pane-border-status", "top"])
    # Three segments, each shown only when present: voice name (bold), program,
    # then the app's own title. So the border reads "sage · claude · ✳ Add help
    # command...", keeping the program visible even after the agent overwrites
    # pane_title with a conversation summary. Missing segments collapse cleanly:
    # name-only -> "sage · ✳ ...", program-only -> "claude · ✳ ...".
    run(["set", "-g", "pane-border-format",
         "#{?@vupai_name,#[bold]#{@vupai_name}#[nobold] · ,}"
         "#{?@vupai_program,#{@vupai_program} · ,}"
         "#{pane_title}"])


def set_terminal_title() -> None:
    """Drive the terminal window/tab title as ``vupai - <session>``.

    tmux ships with ``set-titles`` off, so without this the terminal keeps
    whatever launched it (the bare ``vupai`` command). ``#S`` expands to the
    attached session name, so each session's terminal title is distinct.
    """
    run(["set", "-g", "set-titles", "on"])
    run(["set", "-g", "set-titles-string", "vupai - #S"])


def set_pane_autoname_hooks(self_cmd: str) -> None:
    """Auto-assign a callsign to every newly created pane.

    `self_cmd` is how to invoke this CLI (e.g. "/path/python -m vupai"); tmux
    expands #{pane_id} to the new pane. Output is discarded so splits stay quiet.
    Hooked on split + new-window so manually created panes get named too.
    """
    inner = f"{self_cmd} autoname #{{pane_id}} >/dev/null 2>&1"
    hookcmd = f'run-shell "{inner}"'
    for hook in ("after-split-window", "after-new-window"):
        run(["set-hook", "-g", hook, hookcmd])


def bind_rename_key(self_cmd: str, key: str = "R") -> None:
    """Bind <prefix>+`key` to prompt for a name and apply it to the active pane.

    Lets the user override an auto-assigned callsign from inside any pane,
    without needing a separate shell pane to run `vupai name`.
    """
    inner = f"{self_cmd} name '%%' #{{pane_id}}"
    run(["bind-key", key, "command-prompt", "-p", "rename pane:", f'run-shell "{inner}"'])


def set_base_index() -> None:
    # Number windows and panes from 1, matching the 1-based numbers users speak
    # ("focus two"). Display/UX only: the router resolves spoken numbers by
    # position, so it stays correct regardless of this, but aligning tmux's own
    # numbering keeps `vupai status` and the pane borders consistent with speech.
    run(["set", "-g", "base-index", "1"])
    run(["set", "-g", "pane-base-index", "1"])


def set_extended_keys_off() -> None:
    # Keep the CR from send-keys delivered as a plain Enter so Claude Code
    # submits on it. extended-keys (CSI-u) can re-encode Enter into an escape
    # the TUI does not treat as submit.
    run(["set", "-g", "extended-keys", "off"])


def display_message(pane_id: str, message: str) -> None:
    run(["display-message", "-t", pane_id, message])


def set_status(text: str) -> None:
    """Publish daemon state into the @vupai_status server option, then nudge a
    redraw so the status line updates immediately rather than at status-interval.

    Mirrors the @vupai_name idiom: the value lives in a cheap-to-rewrite user
    option, while `install_status_indicator` wires the format once. The redraw is
    best-effort - a detached server has no client (`refresh-client` then fails),
    but the option still updates for whenever a client attaches."""
    run(["set", "-g", "@vupai_status", text])
    try:
        run(["refresh-client", "-S"])
    except TmuxError:
        pass  # no client attached; the option value is still set


# Marker proving the vupai segment is already in status-right. Lets a re-install
# detect its own output so it prepends idempotently instead of capturing the
# vupai segment as if it were the user's original status-right.
_STATUS_SEGMENT = "#{@vupai_status}"


def show_global(option: str) -> str | None:
    """Return a global option's value, or None when it is unset.

    Uses `show -gv` WITHOUT `-q`: an unset *user* option exits nonzero (tmux:
    "invalid option"), which is how we distinguish 'never saved' from 'saved as
    an empty string' - `-gqv` reports both as empty and would conflate them."""
    try:
        return run(["show", "-gv", option]).rstrip("\n")
    except TmuxError:
        return None


# Compact clock used when the captured original is blank or is tmux's stock
# default (see _status_tail): time only, no date, no pane-title boilerplate.
_CLOCK_TAIL = "%H:%M "

# Focused-pane readout for the MIDDLE of status-right (between the indicator and
# the clock): the active pane's vupai callsign - falling back to its running
# command when unnamed - plus its program label when set. Unlike tmux's window
# list this tracks pane focus, which is what vupai actually navigates; the
# after-select-pane hook (see install_status_indicator) refreshes it on focus
# change. tmux's own window list is hidden (see _hide_window_list).
_PANE_SEGMENT = (
    "#{?@vupai_name,#{@vupai_name},#{pane_current_command}}"
    "#{?@vupai_program, · #{@vupai_program},}"
)

# Date/day tokens that mark tmux's verbose default. A captured original carrying
# one of these AND the stock pane_title boilerplate is the default we replace
# with a compact clock - a genuinely custom status-right is left untouched.
_DATE_TOKENS = ("%d", "%D", "%b", "%B", "%Y", "%y", "%a", "%A", "%j", "%m")


def _status_tail(saved: str) -> str:
    """The clock/tail to render after the vupai segment. Blank or tmux's stock
    default (pane-title + date boilerplate) collapses to a compact time-only
    clock; anything the user genuinely customized is preserved verbatim."""
    if not saved.strip():
        return _CLOCK_TAIL
    if "pane_title" in saved and any(t in saved for t in _DATE_TOKENS):
        return _CLOCK_TAIL  # tmux's verbose default: drop date + pane title
    return saved


def install_status_indicator() -> None:
    """Render the vupai state segment in status-right, PREPENDING it to whatever
    was already there, with the focused-pane readout in the middle and the clock
    at the far right (tmux's real window list hidden).

    The user's original status-right is captured exactly once into
    @vupai_status_orig (the first install, when our segment isn't already
    present), then the line is always rebuilt as
    `<vupai segment>  <pane>  <tail>`, where <tail> is the saved original - or a
    compact time-only clock when that original is blank or tmux's verbose default
    (see _status_tail). Rebuilding from the saved copy - never the already-
    modified live value - makes re-install idempotent (the segment never stacks)
    and reversible (see restore_status_right)."""
    run(["set", "-g", "@vupai_status", "#[fg=green]● vupai#[default]"])

    saved = show_global("@vupai_status_orig")
    if saved is None:
        current = show_global("status-right") or ""
        # Don't capture our own segment as the "original" (legacy install / the
        # clobber this very change fixes) - treat that as no original.
        original = "" if _STATUS_SEGMENT in current else current
        run(["set", "-g", "@vupai_status_orig", original])
        saved = original

    tail = _status_tail(saved).rstrip()
    run(["set", "-g", "status-right",
         f"{_STATUS_SEGMENT}  {_PANE_SEGMENT}  {tail}"])
    _hide_window_list()
    # Redraw the status line on pane focus change so the pane segment tracks
    # selection at once (tmux won't always redraw status on its own).
    run(["set-hook", "-g", "after-select-pane", "refresh-client -S"])

    # Grow (never shrink) the length budget so the prepended segment + original
    # don't truncate; a larger user value is left untouched.
    try:
        current_len = int(show_global("status-right-length") or "0")
    except ValueError:
        current_len = 0
    if current_len < 120:
        run(["set", "-g", "status-right-length", "120"])


def _hide_window_list() -> None:
    """Blank tmux's window list so the only window readout is our status-right
    segment. The user's original formats are captured once into
    @vupai_win_orig / @vupai_wincur_orig so restore_status_right can put them
    back. Idempotent: once captured, the live (already-blanked) value is never
    re-captured."""
    for fmt_opt, save_opt in (
        ("window-status-format", "@vupai_win_orig"),
        ("window-status-current-format", "@vupai_wincur_orig"),
    ):
        if show_global(save_opt) is None:
            run(["set", "-g", save_opt, show_global(fmt_opt) or ""])
        run(["set", "-g", fmt_opt, ""])


def _restore_window_list() -> None:
    """Reverse _hide_window_list: put the user's window-status formats back (or
    unset them to tmux's default) and drop the saved options."""
    for fmt_opt, save_opt in (
        ("window-status-format", "@vupai_win_orig"),
        ("window-status-current-format", "@vupai_wincur_orig"),
    ):
        saved = show_global(save_opt)
        if saved is None:
            continue
        if saved:
            run(["set", "-g", fmt_opt, saved])
        else:
            run(["set", "-gu", fmt_opt])  # revert to tmux's default
        run(["set", "-gu", save_opt])


def restore_status_right() -> None:
    """Reverse install_status_indicator: put the captured original back, restore
    tmux's window list, and drop vupai's options. Used when status_indicator is
    disabled or on teardown. A no-op-ish safe path when nothing was installed."""
    saved = show_global("@vupai_status_orig")
    if saved is not None:
        if saved:
            run(["set", "-g", "status-right", saved])
        else:
            run(["set", "-gu", "status-right"])  # revert to tmux's default
        run(["set", "-gu", "@vupai_status_orig"])
    _restore_window_list()
    run(["set-hook", "-gu", "after-select-pane"])  # drop the pane-refresh hook
    run(["set", "-gu", "@vupai_status"])


def set_tip(text: str) -> None:
    """Publish the current tip into @vupai_tip, then nudge a redraw. Mirrors
    set_status: the value lives in a cheap user option that install_tip_segment
    wired into status-left once. Best-effort redraw (a detached server has no
    client)."""
    run(["set", "-g", "@vupai_tip", text])
    try:
        run(["refresh-client", "-S"])
    except TmuxError:
        pass  # no client attached; the option value is still set


# Marker proving the vupai tip segment is already in status-left (idempotent
# re-install), mirroring _STATUS_SEGMENT for status-right.
_TIP_SEGMENT = "#{@vupai_tip}"


def install_tip_segment() -> None:
    """Render the rotating-tip segment in status-left, APPENDING it after the
    user's original status-left (tmux draws status-left before the window list).

    The original is captured once into @vupai_tip_orig and the line is always
    rebuilt from that saved copy, so re-install is idempotent (the segment never
    stacks) and reversible (see restore_status_left). A blank/unset original
    falls back to tmux's default "[#S] "."""
    saved = show_global("@vupai_tip_orig")
    if saved is None:
        current = show_global("status-left") or ""
        original = "" if _TIP_SEGMENT in current else current
        run(["set", "-g", "@vupai_tip_orig", original])
        saved = original

    head = saved if saved.strip() else "[#S] "
    run(["set", "-g", "status-left", f"{head}  {_TIP_SEGMENT}"])

    try:
        current_len = int(show_global("status-left-length") or "0")
    except ValueError:
        current_len = 0
    if current_len < 80:
        run(["set", "-g", "status-left-length", "80"])


def restore_status_left() -> None:
    """Reverse install_tip_segment: restore the captured original status-left
    and drop vupai's tip options. Safe when nothing was ever installed."""
    saved = show_global("@vupai_tip_orig")
    if saved is not None:
        if saved:
            run(["set", "-g", "status-left", saved])
        else:
            run(["set", "-gu", "status-left"])  # revert to tmux's default
        run(["set", "-gu", "@vupai_tip_orig"])
    run(["set", "-gu", "@vupai_tip"])


def server_running() -> bool:
    try:
        run(["has-session"])
    except TmuxError:
        return False
    return True


def has_session(name: str) -> bool:
    """Whether a session named `name` exists.

    Returns False when the server isn't running too (`has-session` errors),
    so callers can use this as a single "does this session need creating?"
    check without a separate server-up probe.
    """
    try:
        run(["has-session", "-t", f"={name}"])
    except TmuxError:
        return False
    return True


def split_window(target: str, program: str, *,
                 horizontal: bool = False, size: str | None = None) -> str:
    """Split `target` (window or pane id); return the new pane id.

    Empty `program` omits the command so tmux launches the default shell.
    `horizontal=True` (`-h`) splits left/right instead of top/bottom; `size`
    (e.g. "40%") sets the new pane's extent (`-l`). The program stays last so it
    is parsed as the pane command, not a flag value.
    """
    args = ["split-window", "-P", "-F", "#{pane_id}", "-t", target]
    if horizontal:
        args.append("-h")
    if size:
        args += ["-l", size]
    if program:
        args.append(program)
    return run(args).strip()


def select_layout(target: str, layout: str) -> None:
    run(["select-layout", "-t", target, layout])


def kill_pane(pane_id: str) -> None:
    run(["kill-pane", "-t", pane_id])


def select_pane(pane_id: str) -> None:
    run(["select-pane", "-t", pane_id])


def swap_pane(src: str, dst: str, *, detached: bool = False) -> None:
    """Swap two panes. `detached=True` (`-d`) leaves the active pane unchanged.

    The default (no `-d`) preserves the historical behavior used by the voice
    "swap A and B" command. Layout's focus-aware main passes `detached=True` so
    the focused pane lands in the main slot AND stays focused.
    """
    args = ["swap-pane", "-s", src, "-t", dst]
    if detached:
        args.append("-d")
    run(args)


def pane_zoomed(pane_id: str) -> bool:
    """Whether the window containing `pane_id` is currently zoomed."""
    out = run(["display-message", "-p", "-t", pane_id, "#{window_zoomed_flag}"])
    return out.strip() == "1"


def toggle_zoom(pane_id: str) -> None:
    """Toggle the zoom state of `pane_id`'s window (tmux `resize-pane -Z`)."""
    run(["resize-pane", "-Z", "-t", pane_id])


def inside_tmux() -> bool:
    """True when running inside a tmux pane (tmux sets ``$TMUX``).

    Used to avoid a nested-session attach: `tmux attach` from within tmux
    refuses to nest, so callers skip the attach in that case.
    """
    return bool(os.environ.get("TMUX"))


def attach(target: str | None = None) -> None:
    """Replace the current process with ``tmux attach``.

    `target` pins which session to attach to; without it tmux picks the
    most-recently-used session.
    """
    argv = ["tmux", "attach"]
    if target:
        argv += ["-t", f"={target}"]
    os.execvp("tmux", argv)


def switch_client(name: str) -> None:
    """Move the current client to session `name` (used from inside tmux)."""
    run(["switch-client", "-t", f"={name}"])


def kill_session(name: str) -> None:
    """Kill session `name` (exact match). The tmux server/daemon are unaffected."""
    run(["kill-session", "-t", f"={name}"])

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from voxpane import tmuxio


@dataclass(frozen=True)
class Pane:
    id: str          # %N (immutable across the session)
    window_id: str   # @N
    window: str      # window_name
    index: int       # pane_index within its window
    name: str        # voice name (@voxpane_name); == id when unset (unnamed)
    command: str     # pane_current_command
    active: bool     # pane_active "1" -> True


def parse_panes(lines: Iterable[str]) -> list[Pane]:
    """Parse stripped PANE_FORMAT lines (tab-separated) into Pane objects.

    Field order matches tmuxio.PANE_FORMAT:
      pane_id, window_id, window_name, pane_index, @voxpane_name,
      pane_current_command, pane_active
    The voice-name field is empty when the @voxpane_name option is unset; we
    fall back to the pane id there so unnamed panes keep the name == id contract.
    Blank/whitespace-only lines are skipped.
    """
    panes: list[Pane] = []
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            # Malformed row (e.g. a tab inside a name); skip defensively.
            continue
        pane_id, window_id, window, index, name, command, active = parts
        name = name or pane_id  # unset @voxpane_name -> treat as unnamed
        panes.append(
            Pane(
                id=pane_id,
                window_id=window_id,
                window=window,
                index=int(index),
                name=name,
                command=command,
                active=active == "1",
            )
        )
    return panes


class PaneRegistry:
    """Cache of the tmux pane list, refreshed on demand from an injected lister."""

    def __init__(
        self,
        lister: Callable[[], list[str]] = tmuxio.list_panes,
        focuser: Callable[[], str | None] = tmuxio.focused_pane_id,
    ) -> None:
        self._lister = lister
        self._focuser = focuser
        self._panes: list[Pane] = []

    def refresh(self) -> None:
        """Re-read panes from the lister, replacing the cached list."""
        self._panes = parse_panes(self._lister())

    @property
    def panes(self) -> list[Pane]:
        return self._panes

    def focused(self) -> Pane | None:
        """Return the cached Pane whose id matches the focuser, else None."""
        focused_id = self._focuser()
        if focused_id is None:
            return None
        for pane in self._panes:
            if pane.id == focused_id:
                return pane
        return None

    def get(self, name: str) -> Pane | None:
        """Exact, case-insensitive match on Pane.name. First match wins."""
        target = name.casefold()
        for pane in self._panes:
            if pane.name.casefold() == target:
                return pane
        return None

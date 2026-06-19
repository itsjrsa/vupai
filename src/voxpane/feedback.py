from voxpane import tmuxio
from voxpane.router import Route


class Feedback:
    """User-facing feedback: stdout status/errors in the daemon pane, plus
    transient on-screen announcements in the routed target pane."""

    def __init__(self, io=tmuxio) -> None:
        self._io = io

    def status(self, text: str) -> None:
        # Plain status line printed to the daemon pane's stdout.
        print(text)

    def announce(self, route: Route) -> None:
        # Only announce when we actually routed somewhere.
        if route.pane_id is None:
            return
        snippet = route.text[:40]
        if route.matched_name:
            label = f"◀ {route.matched_name}: {snippet}"
        else:
            label = f"◀ (focus): {snippet}"
        self._io.display_message(route.pane_id, label)

    def error(self, text: str) -> None:
        # Error lines are prefixed so they stand out in the daemon pane.
        print(f"error: {text}")

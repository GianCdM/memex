"""Tiny TTY progress rendering — stdlib, zero deps, safe in pipes.

A Progress renders ONE in-place line (carriage return) while work advances,
and erases itself when done — but ONLY when stdout is a real TTY. In pipes
and hooks it is a complete no-op: the harness and logs keep getting the
plain per-item prints, never '\\r' noise. Call sites branch on `.enabled`
to pick between the bar and their verbose prints.
"""

from __future__ import annotations

import shutil
import sys


class Progress:
    """`with Progress("label", total=N) as bar: bar.update(suffix="...")`.

    total=None renders a running counter ("label 12… suffix") for streams of
    unknown length; with a total it renders a bar ("label [####----] 12/40")."""

    def __init__(self, label, total=None, stream=None, enabled=None):
        self.label = label
        self.total = total
        self.n = 0
        self._stream = stream or sys.stdout
        isatty = getattr(self._stream, "isatty", lambda: False)
        self.enabled = bool(isatty()) if enabled is None else enabled
        self._last = ""

    # -- context manager sugar -------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.done()
        return False

    def update(self, n=None, suffix=""):
        self.n = (self.n + 1) if n is None else n
        if not self.enabled:
            return
        self._render(suffix)

    def render_line(self, suffix=""):
        """The line as a string (also used by tests — rendering is pure)."""
        if self.total:
            width = 24
            filled = int(width * min(self.n, self.total) / max(1, self.total))
            core = f"{self.label} [{'#' * filled}{'-' * (width - filled)}] {self.n}/{self.total}"
        else:
            core = f"{self.label} {self.n}…"
        return f"{core} {suffix}".rstrip()

    def _render(self, suffix):
        line = self.render_line(suffix)
        cols = shutil.get_terminal_size((80, 24)).columns
        line = line[: max(10, cols - 1)]
        pad = " " * max(0, len(self._last) - len(line))
        try:
            self._stream.write("\r" + line + pad)
            self._stream.flush()
        except Exception:
            self.enabled = False  # a broken stream must never break the work
        self._last = line

    def done(self):
        """Erase the in-place line so the caller's summary prints cleanly."""
        if self.enabled and self._last:
            try:
                self._stream.write("\r" + " " * len(self._last) + "\r")
                self._stream.flush()
            except Exception:
                pass
        self._last = ""

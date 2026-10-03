"""Terminal progress display: a bar that redraws in place, plus ordinary messages."""

from __future__ import annotations

import sys
import time
from typing import TextIO


class ProgressDisplay:
    """Draws `  fetching [█████░░░░░]  34%  12,650 of 36,890 blocks` on one line.

    On a terminal the bar redraws in place (at most ten times a second). When output
    is redirected to a file, it prints one line per 10% instead, so logs stay short.
    """

    def __init__(self, stream: TextIO | None = None, width: int = 30) -> None:
        self.stream = stream or sys.stderr
        self.width = width
        self.tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.active = False  # a bar line is on screen without a trailing newline
        self.last_draw = 0.0
        self.last_step: tuple[str, int] | None = None

    def message(self, text: str) -> None:
        self.finish()
        print(text, file=self.stream, flush=True)

    def bar(self, label: str, done: int, total: int, detail: str) -> None:
        total = max(total, 1)
        done = min(max(done, 0), total)
        pct = done * 100 // total
        complete = done >= total

        if not self.tty:
            step = (label, pct // 10)
            if step != self.last_step or complete:
                self.last_step = step
                print(f"  {label} {pct:3d}%  {detail}", file=self.stream, flush=True)
            return

        now = time.monotonic()
        if not complete and self.active and now - self.last_draw < 0.1:
            return
        self.last_draw = now
        filled = self.width * done // total
        line = f"  {label:<8} [{'█' * filled}{'░' * (self.width - filled)}] {pct:3d}%  {detail}"
        # \r returns to the start of the line; \033[K clears anything left from before.
        self.stream.write("\r" + line + "\033[K")
        self.stream.flush()
        self.active = True
        if complete:
            self.finish()

    def finish(self) -> None:
        if self.active:
            self.stream.write("\n")
            self.stream.flush()
            self.active = False

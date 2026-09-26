
from __future__ import annotations

import codecs
import os

TAIL_BUFFER_BYTES = 400 * 1024


class TailReader:
    """Bounded incremental reader over a file another process appends to.

    Tracks its own `offset` (bytes already seen) and a bounded trailing
    buffer (`TAIL_BUFFER_BYTES`) of the most recently read text, so a
    terminal block split across two polls' worth of appended bytes still
    matches whole against the buffer. Never raises: any `OSError` (file
    absent, transient stat/read failure, permission issue) is treated as
    "nothing new this poll" and leaves `offset` and the buffer untouched,
    so a caller's poll loop keeps polling rather than crashing or
    false-closing.
    """

    def __init__(self, path: str, *, seek_to_tail: bool = False):
        self._path = path
        self._offset = 0
        self._buffer = ""
        # character is lost PERMANENTLY rather than merely late. An
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line_offset = 0
        self._pending = ""
        self._line_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        if seek_to_tail:
            # poll() reads at most TAIL_BUFFER_BYTES rather than the whole
            try:
                size = os.stat(self._path).st_size
                self._offset = max(size - TAIL_BUFFER_BYTES, 0)
            except OSError:
                pass

    def poll(self) -> str:
        try:
            size = os.stat(self._path).st_size
        except OSError:
            return self._buffer

        if size < self._offset:
            self._offset = 0
            self._buffer = ""
            self._decoder.reset()

        if size == self._offset:
            return self._buffer

        try:
            with open(self._path, "rb") as handle:
                handle.seek(self._offset)
                new_bytes = handle.read(size - self._offset)
        except OSError:
            return self._buffer

        self._offset = self._offset + len(new_bytes)
        self._buffer = (self._buffer + self._decoder.decode(new_bytes))[
            -TAIL_BUFFER_BYTES:
        ]
        return self._buffer

    def poll_lines(self) -> list[str]:
        """Complete lines appended since the last call — the delta, not the buffer.

        For a line-oriented consumer (JSONL), `poll()` is the wrong shape: it
        returns the whole bounded buffer every time, so a caller that parses
        what it gets re-parses up to TAIL_BUFFER_BYTES on every poll and then
        discards the repeats. At a 1s cadence over a 30-minute run that is
        ~1800 full re-parses of the same bytes, and the de-duplication set that
        hides it grows without bound. (Review: overengineering-reviewer #2.)

        This returns only lines completed since the previous call, holding any
        trailing partial line back until its newline arrives. `poll()`'s buffer
        semantics are deliberately left alone: `TerminalWatcher` matches a
        multi-line block that can straddle two reads and genuinely needs the
        window, which is why this is a second method rather than a change to
        the first.

        Shares `poll()`'s shrink handling: on a replaced file the pending
        partial is dropped and lines are re-emitted from the start, so a
        consumer that cares about duplicates still needs its own identity
        check for that path — see `JournalRenderer`.
        """
        try:
            size = os.stat(self._path).st_size
        except OSError:
            return []

        if size < self._line_offset:
            self._line_offset = 0
            self._pending = ""
            self._line_decoder.reset()

        if size == self._line_offset:
            return []

        try:
            with open(self._path, "rb") as handle:
                handle.seek(self._line_offset)
                new_bytes = handle.read(size - self._line_offset)
        except OSError:
            return []

        self._line_offset += len(new_bytes)
        text = self._pending + self._line_decoder.decode(new_bytes)
        parts = text.split("\n")
        self._pending = parts.pop()
        return [
            stripped
            for stripped in (line.rstrip("\r") for line in parts)
            if stripped.strip()
        ]


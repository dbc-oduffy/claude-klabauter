"""
coordinator_core.workflow_watch.tail — bounded incremental tail reader.

Purpose: answer ONE question, shared by `terminal.py`'s matchers and by
`render.py` (C2): "what bytes were appended to this file since I last looked,
and did the file get replaced out from under me?" This is the THIRD and
FOURTH in-repo bounded-tail-read implementation, not the first — see
"Why not reuse" below for why neither existing site fits.

Not a line parser and not a JSON reader: callers get raw decoded text back
and do their own matching over it. No `json.loads`, no subprocess of any
kind — this module only calls `os.stat` and does seek-reads on an already
open path.

Why not reuse `coordinator_core.ops.workflow_fire.fire._read_log_window`:
that function takes ONE snapshot (head window + tail window of whatever the
file currently contains) and has no notion of an `offset` carried between
calls — every call re-reads from byte 0 and from EOF-window. This rebuttal
applies to the watcher's callers (`terminal.py`, `render.py`), which poll
the SAME file repeatedly over the life of a run and must only
see bytes appended since the previous poll, or they cannot distinguish "the
terminal record was already there five polls ago" from "it just landed" —
`_read_log_window` has no mechanism for that distinction at all.

Why not reuse `coordinator_core.hooks.subagent_arrival_check._read_last_nonempty_line`:
that function walks backward from EOF on every call, looking for the LAST
line only, and is happy to re-derive the same answer from the same trailing
bytes on every poll — it has no `offset` state either, and it explicitly
discards everything before the last line. This watcher's terminal block can
appear anywhere in newly appended bytes (not necessarily the file's last
line by the time of the next poll — more events can land after it before a
poll notices), and a shrink (PreCompact/PostCompact rewriting the session
transcript out from under a live reader — see the plan's Platform
assumptions section) has to reset an `offset`, not a backward walk, so
re-scanning finds content again rather than silently reading nothing new.
Both gaps are exactly the shrink-reset and task-id-keyed matching
requirements this module exists to satisfy.
"""

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
        # One incremental decoder per reader. A plain bytes.decode() per
        # chunk corrupts any multi-byte character straddling a read
        # boundary -- with errors="replace" the leading bytes become U+FFFD
        # immediately and the rest of the sequence arrives orphaned, so the
        # character is lost PERMANENTLY rather than merely late. An
        # incremental decoder holds those trailing bytes until the next read
        # completes them. (Review: code-reviewer slice 1, P1.)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        # poll_lines() tracks its own position: the two readers advance
        # independently, so one consumer cannot starve the other.
        self._line_offset = 0
        self._pending = ""
        self._line_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        if seek_to_tail:
            # One-shot bounded-tail snapshot: seed the offset so the FIRST
            # poll() reads at most TAIL_BUFFER_BYTES rather than the whole
            # file from byte 0. For a caller that polls exactly once (a
            # hot-path hook taking a single snapshot, never a watcher that
            # polls the same file repeatedly), this is the difference
            # between a bounded tail read and a whole-file read — the
            # carried offset and shrink-reset machinery below are unused
            # either way for a one-shot caller.
            try:
                size = os.stat(self._path).st_size
                self._offset = max(size - TAIL_BUFFER_BYTES, 0)
            except OSError:
                pass

    def poll(self) -> str:
        """Read newly appended bytes since the last poll and return the
        current bounded trailing buffer (decoded text, not just the new
        bytes) — the buffer is what callers should match against, since a
        terminal block can straddle two polls' worth of appended bytes.

        Shrink path: if the file's current size is smaller than `offset`,
        the file has been replaced out from under this reader (a
        compacted/rewritten transcript is the documented case — see the
        module docstring). Treated as a full replacement: `offset` resets
        to 0 and the file is re-scanned from the start. This is safe for
        every matcher this module feeds because matching is done by a
        stable identity (task id), not by stream position — a re-scan
        cannot cause a matcher to fire twice on the SAME underlying event
        going undetected; at most it re-observes bytes already seen once.

        Never raises. On any `OSError` (path absent, transient stat/read
        failure), returns the buffer unchanged and does not advance
        `offset` — the caller's next poll simply tries again.
        """
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
        # Split on "\n" rather than str.splitlines(): splitlines() also
        # breaks on , , - and U+2028/2029, any of which can
        # appear INSIDE a JSON string value and would silently cut a record
        # in half. Strip a trailing "\r" per line so a CRLF journal --
        # the ordinary case on Windows, which is first-class here -- yields
        # the same lines as a LF one.
        parts = text.split("\n")
        self._pending = parts.pop()
        return [
            stripped
            for stripped in (line.rstrip("\r") for line in parts)
            if stripped.strip()
        ]


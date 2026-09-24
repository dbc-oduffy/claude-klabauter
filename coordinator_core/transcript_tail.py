"""coordinator_core.transcript_tail -- shared bounded tail-read of a Claude
Code session transcript (`.jsonl`), and the one derived fact read from it:
the most recent `type == "assistant"` record's `message.model`, used by
`coordinator_core.hooks.block_ungranted_opus_subagent` to resolve the parent
session's model.

ZERO SPAWN, BOUNDED READ (DR-344): reads at most `_TAIL_MAX_CHUNKS *
_TAIL_CHUNK_BYTES` (~256KiB) from EOF, never the whole file, and never
shells out.
"""

from __future__ import annotations

import json
import os
from typing import Any, List, Optional

#: 8KiB chunks, capped at 32 chunks (~256KiB) from EOF -- verbatim parity
#: with `hooks/subagent_arrival_check.py`'s own bounded-read constants, the
#: shape this module generalizes to multiple trailing lines.
_TAIL_CHUNK_BYTES = 8192
_TAIL_MAX_CHUNKS = 32
_TAIL_SCAN_MAX_LINES = 64


def tail_lines(path: str, *, max_lines: int = _TAIL_SCAN_MAX_LINES) -> List[str]:
    """Bounded tail read of `path`, returning up to `max_lines` trailing
    non-empty lines in on-disk order. Returns `[]` on any failure (absent
    file, OSError, empty file) -- never raises."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            if file_size == 0:
                return []

            buf = b""
            pos = file_size
            chunks_read = 0
            while pos > 0 and chunks_read < _TAIL_MAX_CHUNKS:
                read_size = min(_TAIL_CHUNK_BYTES, pos)
                pos -= read_size
                fh.seek(pos)
                buf = fh.read(read_size) + buf
                chunks_read += 1
                if buf.count(b"\n") > max_lines or pos == 0:
                    break
    except OSError:
        return []

    text = buf.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if pos > 0 and lines:
        # The first recovered line may be a fragment (the chunk boundary
        # landed mid-line) unless the read reached the true start of the
        # file (pos == 0) -- drop it rather than risk a false JSON-parse
        # failure on a partial line.
        lines = lines[1:]
    return lines[-max_lines:]


def resolve_last_assistant_model(transcript_path: Any) -> Optional[str]:
    """Return the most recent `type == "assistant"` record's
    `message.model` field from `transcript_path`, scanning the bounded tail
    (see `tail_lines`) newest-first. Returns `None` on: no/unreadable
    `transcript_path`, or no qualifying record found within the tail window
    -- the last on-disk record is not reliably an assistant turn (it may be
    a tool_result/user record), which is why this scans a window rather than
    only the literal last line. Callers must treat `None` as "unresolved",
    never as "the model is empty"."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    for line in reversed(tail_lines(transcript_path)):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return None

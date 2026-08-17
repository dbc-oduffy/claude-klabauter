"""
coordinator_core.session.context_usage_sidecar — single-source resolver,
writer, and reader for the per-session context-usage sidecar.

Claude Code 2.1.234 hands the statusline command's stdin an authoritative
``context_window`` block (``used_percentage``, ``remaining_percentage``,
``context_window_size``, and a ``current_usage`` breakdown of
``input_tokens`` / ``output_tokens`` / ``cache_creation_input_tokens`` /
``cache_read_input_tokens``). No hook event payload carries any of it, so a
pass-through statusline writes that block to this sidecar and the
PostToolUse context-pressure advisory reads it back — this module is that
producer/consumer contract, and the only place either side may resolve the
sidecar path.

Spec backlink: C1 of the 2026-08-17 "the advisory reads the harness" plan
(``docs/plans/2026-08-17-the-advisory-reads-the-harness.md``). The writer is
``coordinator/bin/statusline.py`` (C3); the reader is
``coordinator_core/hooks/postuse_advisory_dispatch.py`` (C4).

Negative-spec:
    - Do NOT hardcode ``/tmp`` or reach for ``tempfile.gettempdir()``
      directly at a new call site for this sidecar — import and call
      ``sidecar_path()`` so there is exactly one place this convention can
      drift again. Mirrors the same Windows-tempdir lesson recorded in
      ``coordinator_core/session/autonomous_sentinel.py``: a hardcoded POSIX
      ``/tmp`` silently splits writer and reader onto different paths on
      Windows.
    - ``age_seconds`` on a ``UsageReading`` is REPORTED, never used here to
      suppress or discard a reading. Staleness POLICY (whether/how a stale
      reading changes advisory behaviour) belongs to a later chunk (C4) —
      this module only measures and reports age, it does not decide.
    - ``now`` is an explicit injected parameter on both ``write_usage`` and
      ``read_usage`` rather than read from the clock inside either function,
      so callers (and their tests) are deterministic without monkeypatching
      time.
    - No chunk in this module owns SessionEnd cleanup of the sidecar file,
      and that is deliberate, not an oversight: the file lives in the
      platform tempdir, the same as the autonomous-run sentinel, and is left
      for the OS tempdir cleaner to reap on its own schedule. Adding a
      SessionEnd deletion hook here is out of scope for this module.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SIDECAR_PREFIX = "context-usage-"

# In-process memo of the last serialised block this process wrote, keyed by
# sidecar path. Write elision: under this repo's 50-70 concurrent-session
# load norm, a create/write/rename on every statusline render into a shared
# tempdir is worth skipping when nothing changed. The statusline process is
# long-lived per session, so an in-process memo (rather than a re-read of
# the file on disk) is sufficient and cheaper.
_last_written: dict[Path, bytes] = {}


def sidecar_path(session_id: str) -> Path:
    """Return the context-usage sidecar path for ``session_id``.

    Resolves the platform temp directory via ``tempfile.gettempdir()``
    (honours TMPDIR/TEMP/TMP per-platform) rather than a hardcoded POSIX
    ``/tmp`` — the single point of truth for both the sidecar writer and
    every reader.
    """
    return Path(tempfile.gettempdir()) / f"{_SIDECAR_PREFIX}{session_id}"


@dataclass(frozen=True)
class UsageReading:
    """A sidecar reading: the harness's ``context_window`` block plus the
    wall-clock age of the stamp it was written with, computed against the
    ``now`` the caller supplied to `read_usage`."""

    context_window: dict[str, Any]
    age_seconds: float


def write_usage(session_id: str, context_window_block: dict[str, Any], *, now: float) -> None:
    """Serialise ``context_window_block`` plus a wall-clock stamp of ``now``
    to the sidecar for ``session_id``, atomically.

    Writes a temp file in the same directory as the target, then
    ``os.replace``s it into place, so a concurrent reader never observes a
    half-written record — the writer fires on every statusline render and
    the reader on every throttled PostToolUse, so torn reads are a real
    concurrency case, not a theoretical one.

    Elides the write entirely when the serialised block is byte-identical to
    the last one this process wrote for this path (see module-level
    ``_last_written``).
    """
    target = sidecar_path(session_id)
    serialised_block = json.dumps(context_window_block, sort_keys=True).encode("utf-8")

    if _last_written.get(target) == serialised_block:
        return

    payload = {"context_window": context_window_block, "stamp": now}
    serialised_payload = json.dumps(payload, sort_keys=True).encode("utf-8")

    tmp_path = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_bytes(serialised_payload)
    os.replace(tmp_path, target)

    _last_written[target] = serialised_block


def read_usage(session_id: str, *, now: float) -> UsageReading | None:
    """Read the sidecar for ``session_id``, returning ``None`` when the file
    is absent or unparseable.

    On success, returns a ``UsageReading`` carrying the harness's
    ``context_window`` block verbatim and ``age_seconds`` computed as
    ``now`` minus the stamp recorded at write time. ``age_seconds`` is
    reported only — this function applies no staleness policy.
    """
    target = sidecar_path(session_id)

    try:
        raw = target.read_bytes()
    except OSError:
        return None

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    context_window = payload.get("context_window")
    stamp = payload.get("stamp")

    if not isinstance(context_window, dict):
        return None
    if not isinstance(stamp, (int, float)):
        return None

    return UsageReading(context_window=context_window, age_seconds=now - stamp)

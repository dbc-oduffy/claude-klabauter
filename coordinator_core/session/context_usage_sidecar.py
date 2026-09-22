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
    - Do NOT hardcode the sidecar location at a new call site — import and
      call ``sidecar_path()`` so there is exactly one place this convention
      can drift again. The lesson this module learned the expensive way is
      not only the Windows-tempdir one recorded in
      ``coordinator_core/session/autonomous_sentinel.py``: a reader and a
      writer can each be individually correct and still name different files,
      and nothing fails loudly when they do — the advisory just reports
      UNKNOWN forever. One resolver, both ends.
    - ``age_seconds`` on a ``UsageReading`` is REPORTED, never used here to
      suppress or discard a reading. Staleness POLICY (whether/how a stale
      reading changes advisory behaviour) belongs to a later chunk (C4) —
      this module only measures and reports age, it does not decide.
    - ``now`` is an explicit injected parameter on both ``write_usage`` and
      ``read_usage`` rather than read from the clock inside either function,
      so callers (and their tests) are deterministic without monkeypatching
      time.
    - No chunk in this module owns SessionEnd cleanup of the sidecar file,
      and that is deliberate, not an oversight: the file is the producer's
      to reap, under the producer's settings home, and it is swept with the
      rest of session scratch. Adding a SessionEnd deletion hook here is out
      of scope for this module.

`read_usage`'s `transcript_path` fallback (2026-09-19, "tell a cloud EM
compaction is inbound" C1): a cloud session registers no statusline, so the
sidecar this module reads is never written there. When no sidecar record
exists, `read_usage` derives a reading from the transcript JSONL the hook
payload already carries a path to — never a constructed home-relative path.
A usable sidecar record still wins unconditionally. The two sources do NOT
produce the same block shape, though — the sidecar carries `used_percentage`
and `context_window_size`, the transcript carries `total_input_tokens` and no
window at all — and that divergence is why the consumer needs a resolution
step (`postuse_advisory_dispatch._used_tokens_and_display_pct`) instead of one
path. Converging the shapes here would retire that step; until then, this is
two readers behind one entry point, not one reader with a branch.

Negative-spec, transcript fallback:
    - Do NOT sum `usage` across every content-block line of one API response
      — every block of one response repeats that response's `usage`
      verbatim, and summing over-counts. Dedupe by `message.id`.
    - Do NOT trust a streaming-placeholder row: `input_tokens` in {0, 1}
      with no cache figures is mid-stream, not a settled tally.
    - Do NOT read the whole file. A bounded 256 KiB binary tail read costs
      1.6 ms against 6.0 ms for a full-file scan on a 1 MB+ transcript, for
      an identical result — seek in binary and discard the first (likely
      partial) line.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SETTINGS_HOME_ENV = "COORDINATOR_SETTINGS_HOME"
_DEFAULT_SETTINGS_HOME = ".coordinator-claude-settings"
_SIDECAR_SUBDIR = ("state", "context-window")

# In-process memo of the last serialised block this process wrote, keyed by
# sidecar path. Write elision: under this repo's 50-70 concurrent-session
# load norm, a create/write/rename on every statusline render into a shared
# settings home is worth skipping when nothing changed. The statusline process is
# long-lived per session, so an in-process memo (rather than a re-read of
# the file on disk) is sufficient and cheaper.
_last_written: dict[Path, bytes] = {}


def _safe_stem(session_id: str) -> str:
    """Sanitise ``session_id`` into a filename stem, matching the producer's
    own ``_safe_stem`` byte-for-byte — the two must agree or they name
    different files for the same session."""
    return "".join(c for c in session_id if c.isalnum() or c in "-_")


def sidecar_path(session_id: str) -> Path:
    """Return the context-usage sidecar path for ``session_id``.

    Resolves DoE-claude's `coordinator/bin/statusline.py` record — the sole
    live producer, registered as this machine's `statusLine`. It publishes to
    the settings home's ``state/context-window/`` directory, one
        ``<session_id>.json`` per session
    (default settings home ``~/.coordinator-claude-settings``), and that path
    plus the record shape below is a cross-plane contract: DoE's withdrawal-memo
    exchange settled it as theirs to hold stable and ours to read.

    Negative-spec: this is NOT a tempdir path. It was one, resolved via
    ``tempfile.gettempdir()``, matched to a claude-klabauter-side producer that was
    withdrawn before it ever shipped — leaving this reader pointed at a file
    nothing writes, and the context-pressure advisory reporting UNKNOWN in
    every interactive session on the machine. Do not "restore" the tempdir
    convention; there is no producer at the other end of it.
    """
    home = os.environ.get(_SETTINGS_HOME_ENV)
    root = Path(home) if home else Path.home() / _DEFAULT_SETTINGS_HOME
    return root.joinpath(*_SIDECAR_SUBDIR) / f"{_safe_stem(session_id)}.json"


@dataclass(frozen=True)
class UsageReading:
    """A sidecar reading: the harness's ``context_window`` block plus the
    wall-clock age of the stamp it was written with, computed against the
    ``now`` the caller supplied to `read_usage`."""

    context_window: dict[str, Any]
    age_seconds: float


def write_usage(session_id: str, context_window_block: dict[str, Any], *, now: float) -> None:
    """Serialise ``context_window_block`` plus a wall-clock ``captured_at``
    stamp of ``now`` to the sidecar for ``session_id``, atomically.

    The live producer is DoE-claude's statusline, not this function; this
    writer exists so tests and any future claude-klabauter-side producer emit the exact
    record shape `read_usage` consumes, rather than a second convention.

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

    payload = {"context_window": context_window_block, "captured_at": now}
    serialised_payload = json.dumps(payload, sort_keys=True).encode("utf-8")

    # Judged, not overlooked: this sidecar lives under
    # ``$COORDINATOR_SETTINGS_HOME``, not the session hub — one of the three
    # session-id-keyed corpora core.ensure_session's negative-spec names as
    # explicitly NOT sessions. Minting a meta.json here is the inverse defect.
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_bytes(serialised_payload)
    os.replace(tmp_path, target)

    _last_written[target] = serialised_block


_TRANSCRIPT_TAIL_BYTES = 256 * 1024


def read_usage(
    session_id: str, *, now: float, transcript_path: str | None = None
) -> UsageReading | None:
    """Read the sidecar for ``session_id``, returning ``None`` when the file
    is absent or unparseable AND no transcript fallback is usable either.

    On success, returns a ``UsageReading`` carrying the harness's
    ``context_window`` block verbatim and ``age_seconds`` computed as
    ``now`` minus the ``captured_at`` stamp recorded at write time. ``age_seconds`` is
    reported only — this function applies no staleness policy.

    When no sidecar record exists, falls back to `transcript_path` (the hook
    payload's own path, never constructed) — see the module docstring's
    "transcript_path fallback" section. A usable sidecar record always wins;
    the fallback only runs when the sidecar branch itself returns ``None``.
    """
    sidecar_reading = _read_sidecar_usage(session_id, now=now)
    if sidecar_reading is not None:
        return sidecar_reading
    if transcript_path:
        return _read_usage_from_transcript(transcript_path, now=now)
    return None


def _read_sidecar_usage(session_id: str, *, now: float) -> UsageReading | None:
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
    captured_at = payload.get("captured_at")

    if not isinstance(context_window, dict):
        return None
    if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)):
        return None

    return UsageReading(context_window=context_window, age_seconds=now - captured_at)


def _read_usage_from_transcript(transcript_path: str, *, now: float) -> UsageReading | None:
    """Derive a `UsageReading` from a session transcript JSONL's own latest
    assistant-message `usage`, when no sidecar record exists.

    Returns ``None`` on any I/O failure or when no usable usage row is found
    in the tail — never raises. The resulting `context_window` carries
    ``total_input_tokens`` and a ``current_usage`` breakdown, but no
    ``used_percentage``/``context_window_size`` — the transcript alone
    cannot name the window it is a fraction of; the caller is responsible for
    resolving that (see `postuse_advisory_dispatch`'s threshold derivation).
    ``age_seconds`` is 0.0: this is a live read of the file, not a stamped
    record.
    """
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - _TRANSCRIPT_TAIL_BYTES)
            fh.seek(start)
            tail = fh.read()
    except OSError:
        return None

    if start > 0:
        # The seek almost certainly landed mid-line; discard that partial
        # first line rather than risk parsing a truncated JSON object.
        _, _, tail = tail.partition(b"\n")

    try:
        text = tail.decode("utf-8", errors="ignore")
    except Exception:
        return None

    last_message_id: Any = None
    last_usage: dict[str, Any] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        message_id = message.get("id")
        if not isinstance(usage, dict) or not message_id:
            continue

        input_tokens = usage.get("input_tokens")
        cache_creation = usage.get("cache_creation_input_tokens")
        cache_read = usage.get("cache_read_input_tokens")
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, (int, float)):
            continue
        if input_tokens in (0, 1) and not cache_creation and not cache_read:
            # Streaming-placeholder row -- not a settled usage figure.
            continue
        if message_id == last_message_id:
            # Another content block of the same response; already counted.
            continue

        last_message_id = message_id
        last_usage = usage

    if last_usage is None:
        return None

    input_tokens = last_usage.get("input_tokens") or 0
    cache_creation = last_usage.get("cache_creation_input_tokens") or 0
    cache_read = last_usage.get("cache_read_input_tokens") or 0
    try:
        total_input_tokens = int(input_tokens) + int(cache_creation) + int(cache_read)
    except (TypeError, ValueError):
        return None

    context_window = {
        "total_input_tokens": total_input_tokens,
        "current_usage": {
            "input_tokens": int(input_tokens),
            "cache_creation_input_tokens": int(cache_creation),
            "cache_read_input_tokens": int(cache_read),
        },
    }
    return UsageReading(context_window=context_window, age_seconds=0.0)

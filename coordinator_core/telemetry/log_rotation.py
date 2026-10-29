"""
coordinator_core.telemetry.log_rotation — generic size-based cascade
rotation for unbounded append-only sinks under
``<git_common_dir>/coordinator-sessions/logs/``.

Purpose: five sinks in that directory
(``coordinator_core.telemetry.op_latency``'s ``op-latency.jsonl``,
``coordinator_core.hooks.agent_completion_log``'s ``agent-audit.jsonl``,
the PostToolUse canary's ``posttooluse-agent-canary.log``,
``coordinator_core.ops.session.safe_commit_offer``'s
``sessionend-auto-commit-diagnostics.log``, and
``coordinator_core.telemetry.host_sampler``'s ``host-samples.jsonl``) are
append-only with NO rotation and NO retention policy of any kind (2026-08-15 fleet-degradation
forensics, state/audits/2026-08-15-fleet-degradation-forensics.md — the
only reason that incident was reconstructable at all was
``op-latency.jsonl``, then 51 MB / 222,572 records / ~7 days, one
``git gc`` or disk-pressure sweep from gone). This module is the one
rotation primitive all four share, rather than four independently
maintained copies of the same cascade logic.

Rotation preserves history; it never truncates. A generation cascade
(``X`` -> ``X.1`` -> ``X.2`` -> ... -> ``X.K``) via ``os.replace``, which is
atomic on both POSIX and Windows (unlike the plain-``O_APPEND`` hazard
documented in ``coordinator_core.atomic_append`` — ``os.replace`` is a
different, genuinely-atomic-everywhere primitive: MoveFileExW with
``MOVEFILE_REPLACE_EXISTING`` on Windows, ``rename(2)`` on POSIX). Deleting
only the oldest generation once ``K`` is exceeded is the ONLY deletion this
module ever performs — discarding the CURRENT file's content on rotation is
the exact defect being fixed, never the goal.

Size/generation arithmetic (state your numbers, don't just assert them):
    Observed op-latency growth: 51 MB / ~7 days ~= 7.3 MB/day at the
    documented 50-70-concurrent-session load norm (docs/wiki/machine-load-
    norm.md). ``_ROTATE_THRESHOLD_BYTES = 25 MiB`` sizes one generation to
    roughly 3-4 days of that traffic — long enough that a single incident
    window (the 2h28m blackout this module exists to prevent recurrence of)
    sits comfortably inside ONE generation, never split across a rotation
    boundary. ``_MAX_GENERATIONS = 4`` (current file + 3 rotated) keeps
    roughly 12-16 days of op-latency history on disk at ~100 MiB worst case
    for that one sink — meaningfully more than one incident window without
    being unbounded. Disk is cheap; unbounded-forever is the defect being
    fixed, not "keep everything." The three smaller sinks (528 KB / 194 KB /
    82 KB observed) rotate under the same constants and will, in practice,
    almost never hit the threshold — that's fine, the primitive is sized
    against the worst-observed sink, not tuned per-sink.

Negative-spec (hard-won):
    - Never a per-append size check. The append path reaches rotation only
      through ``rotate_on_cadence`` (``op_latency._write_entry``): one dict
      lookup per append, the stat sweep at most once per cadence per dir per
      process. Calling ``rotate_if_needed`` directly inline with a write
      would cost a stat() on every ~85ms-floor invocation.
    - Never raises. Every public entry point here swallows every exception
      (unwritable dir, disk full, a permission error, a vanished file, a
      concurrent renamer) to a debug log and returns, mirroring
      ``op_latency``'s own "Never breaks dispatch" contract verbatim. A
      retention defect must never fail the caller's op or ceremony.
    - Concurrency under 50-70 writers: ``os.replace`` is atomic, so a
      rotation and a concurrent appender can never observe a torn or
      half-renamed file. The brief window this DOES admit: an appender
      that already has the sink path resolved may open/write to the
      just-renamed-away inode (now ``X.1``) a moment after this module
      renamed it. That write is NOT lost -- it lands in the rotated
      generation instead of the fresh ``X``, one file over from where it
      "should" be, and is later reassembled by any reader (like
      ``op_latency.pairing_summary``) that already treats "kind" as a
      correctness signal independent of which generation a row lives in.
      This mirrors ``atomic_append``'s own precedent: every append opens,
      writes, and closes the sink fresh (no long-lived file descriptors
      anywhere in this repo's append callers), so the next append after a
      rotation simply recreates ``X`` from scratch rather than being
      stranded on a rotated inode.
    - Cheap on the no-op path: a rotation check that finds the sink under
      threshold (the overwhelmingly common case) costs exactly one
      ``os.stat`` and returns -- no directory listing, no generation
      cascade touched.

Spec backlink: state/audits/2026-08-15-fleet-degradation-forensics.md
               docs/wiki/machine-load-norm.md
"""

from __future__ import annotations

import os
import time
from pathlib import Path

_ROTATE_THRESHOLD_BYTES = 25 * 1024 * 1024

_MAX_GENERATIONS = 4

_logger = None


def _log():
    global _logger
    if _logger is None:
        import logging
        _logger = logging.getLogger(__name__)
    return _logger


def _generation_path(sink: Path, generation: int) -> Path:
    if generation == 0:
        return sink
    return sink.with_name(f"{sink.stem}.{generation}{sink.suffix}")


def rotate_if_needed(
    sink: Path,
    *,
    threshold_bytes: int = _ROTATE_THRESHOLD_BYTES,
    max_generations: int = _MAX_GENERATIONS,
) -> bool:
    try:
        try:
            size = sink.stat().st_size
        except OSError:
            return False

        if size < threshold_bytes:
            return False

        oldest = _generation_path(sink, max_generations)
        try:
            oldest.unlink()
        except OSError:
            pass

        for generation in range(max_generations - 1, 0, -1):
            src = _generation_path(sink, generation)
            dst = _generation_path(sink, generation + 1)
            try:
                os.replace(src, dst)
            except OSError:
                continue

        try:
            os.replace(sink, _generation_path(sink, 1))
        except OSError:
            return False

        return True
    except Exception:
        try:
            _log().debug(
                "coordinator_core.telemetry.log_rotation: rotate_if_needed failed for %r",
                sink, exc_info=True,
            )
        except Exception:
            pass
        return False


_KNOWN_SINK_NAMES = (
    "op-latency.jsonl",
    "agent-audit.jsonl",
    "posttooluse-agent-canary.log",
    "sessionend-auto-commit-diagnostics.log",
    "host-samples.jsonl",
)


def rotate_all_known_sinks(
    logs_dir: Path,
    *,
    threshold_bytes: int = _ROTATE_THRESHOLD_BYTES,
    max_generations: int = _MAX_GENERATIONS,
) -> int:
    """Rotate every sink in ``_KNOWN_SINK_NAMES`` that lives under
    ``logs_dir`` and is at or above ``threshold_bytes``.

    Intended cadence-site entry point -- see
    ``coordinator_core.bash_guards._advisory_dedupe``'s
    ``_maybe_sweep_stale_session_dirs`` for the throttled call site this was
    wired to. Never raises (each sink's ``rotate_if_needed`` call already
    swallows its own exceptions; this function does not add new failure
    modes). Returns the count of sinks actually rotated.
    """
    rotated = 0
    for name in _KNOWN_SINK_NAMES:
        try:
            if rotate_if_needed(
                logs_dir / name,
                threshold_bytes=threshold_bytes,
                max_generations=max_generations,
            ):
                rotated += 1
        except Exception:
            try:
                _log().debug(
                    "coordinator_core.telemetry.log_rotation: rotate_all_known_sinks "
                    "failed for %r", name, exc_info=True,
                )
            except Exception:
                pass
    return rotated


_CADENCE_SECS = 60.0

_last_checked: dict = {}


def rotate_on_cadence(
    logs_dir: Path,
    *,
    cadence_secs: float = _CADENCE_SECS,
    now: "float | None" = None,
) -> int:
    """Rotate every known sink in ``logs_dir``, at most once per ``cadence_secs``
    per directory per process; an in-window call costs one dict lookup.

    The append path's entry point: reached by the process that writes the
    sink, so no unrelated sweep has to cover this directory. Never raises.
    """
    try:
        t = time.monotonic() if now is None else now
        key = str(logs_dir)
        last = _last_checked.get(key)
        if last is not None and 0 <= t - last < cadence_secs:
            return 0
        _last_checked[key] = t
        return rotate_all_known_sinks(logs_dir)
    except Exception:
        return 0

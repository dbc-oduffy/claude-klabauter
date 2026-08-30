"""
coordinator_core.workflow_watch — the watcher entry point an EM's Monitor call runs.

Purpose: `python3 -m coordinator_core.workflow_watch` polls a launching session
transcript and a run journal for one background `Workflow` task until that
task reaches a terminal state OR this module's own wall-clock cap elapses —
whichever comes first. It is the mechanism that makes
`docs/plans/2026-08-30-the-workflow-monitor-outlives-the-run-it-watches.md`'s
exit criterion true: the watcher is bounded by its own enforced cap,
independent of whatever `timeout_ms` a model retyped into the Monitor call
that launched it.

This module owns wiring together three already-scoped pieces, not their own
logic: `terminal.TerminalWatcher` (has task id T's run ended — see
`terminal.py`'s own module docstring for the two matchers and the fail-safe
guarantee), `tail.TailReader` (the bounded incremental reader both `terminal`
and `render` share), and `render` (C2 — renders `journal.jsonl` into one
short stdout line per event).

Negative-spec: this module does not derive a transcript or journal path from
projects-root/project-slug/session-id — reconstructing
`~/.claude/projects/<slug>/<session>.jsonl` from parts re-encodes a
single-machine path convention this module does not own (see the plan's
Anti-scope). Both paths arrive verbatim via argv. It does not decide whether
a run is "done" by any journal balance check (`started == result + failed`)
— that is the plan's named false-close vector; only `TerminalWatcher`'s two
positively-matched record shapes end a poll loop with exit code 0.
"""

from __future__ import annotations

import argparse
import sys
import time

from coordinator_core.workflow_watch.render import JournalRenderer
from coordinator_core.workflow_watch.terminal import TerminalWatcher

# The spike's measured basis: 7.8 microseconds/poll, ~14ms of process time
# across a 30-minute run at this cadence.
DEFAULT_POLL_INTERVAL_SECONDS = 1.0

# The wall-clock cap this watcher enforces on itself, independent of any
# timeout_ms a model may have retyped into the Monitor call that launched
# it (see the plan's persistent-arming bullet and prime_exit_criterion).
# Matches the spike's own 30-minute measurement window above.
#
# DEFAULT_CAP_MS is this same bound in milliseconds, DERIVED from the seconds
# value rather than restated, so the two cannot drift apart. The units are not
# interchangeable and the split is not cosmetic: this module's `--cap` argv is
# in SECONDS, while the `timeout_ms` the PostToolUse advisory (C4) emits into a
# Monitor call is in MILLISECONDS. C4 imports DEFAULT_CAP_MS for the Monitor
# field and DEFAULT_CAP_SECONDS for the `--cap` it writes into the command line.
DEFAULT_CAP_SECONDS = 30 * 60
DEFAULT_CAP_MS = int(DEFAULT_CAP_SECONDS * 1000)


def _make_renderer(journal_path: str):
    """Construct C2's journal renderer.

    `render.py` is a sibling module in this same package, landed in the
    same commit as this file — not an optional dependency, so this is a
    plain construction, not a best-effort import.
    """
    return JournalRenderer(journal_path)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m coordinator_core.workflow_watch",
        description=(
            "Poll a launching session transcript and run journal for one "
            "background Workflow task until it reaches a terminal state or "
            "this watcher's own wall-clock cap elapses."
        ),
    )
    parser.add_argument(
        "--transcript",
        required=True,
        help=(
            "Absolute path to the launching session transcript, taken "
            "verbatim from the hook's transcript_path — no path is "
            "reconstructed from parts."
        ),
    )
    parser.add_argument(
        "--journal",
        required=True,
        help=(
            "Absolute path to the run's journal.jsonl, derivable from "
            "transcriptDir+runId in the launch result."
        ),
    )
    parser.add_argument(
        "--task-id",
        required=True,
        help="The harness TASK id to match terminal records against (never the wf_ run id).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Seconds between polls (default: %(default)s).",
    )
    parser.add_argument(
        "--cap",
        type=float,
        default=DEFAULT_CAP_SECONDS,
        help="Wall-clock cap in seconds this watcher enforces on itself (default: %(default)s).",
    )
    return parser.parse_args(argv)


def _watch(
    transcript_path: str,
    journal_path: str,
    task_id: str,
    poll_interval: float,
    cap_seconds: float,
) -> int:
    """Poll loop. Returns the process exit code per this module's contract:

    - `0` — a terminal record was observed; which of the four statuses it
      was is printed on the terminal stdout line, never encoded in the
      exit code (`failed`/`killed` are real detections, not give-ups).
    - `1` — the wall-clock cap was reached without a terminal record;
      distinguishable from a real terminal exit by exit code alone, so a
      Monitor consumer never has to parse stdout to tell "the run ended"
      from "I gave up."
    """
    watcher = TerminalWatcher(transcript_path, task_id)
    renderer = _make_renderer(journal_path)
    deadline = time.monotonic() + cap_seconds

    while True:
        if renderer is not None:
            for line in renderer.poll():
                print(line)
                sys.stdout.flush()

        status = watcher.check()
        if status is not None:
            print(f"terminal: {status}")
            sys.stdout.flush()
            return 0

        if time.monotonic() >= deadline:
            print(
                f"cap reached ({cap_seconds}s) without a terminal record for task {task_id}",
                file=sys.stderr,
            )
            return 1

        time.sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return _watch(
        transcript_path=args.transcript,
        journal_path=args.journal,
        task_id=args.task_id,
        poll_interval=args.poll_interval,
        cap_seconds=args.cap,
    )


if __name__ == "__main__":
    sys.exit(main())

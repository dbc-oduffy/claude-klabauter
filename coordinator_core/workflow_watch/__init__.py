
from __future__ import annotations

import argparse
import sys
import time

from coordinator_core.workflow_watch.render import JournalRenderer
from coordinator_core.workflow_watch.stamp import reconcile as _reconcile_run
from coordinator_core.workflow_watch.stamp import stamp_terminal
from coordinator_core.workflow_watch.terminal import TerminalWatcher

DEFAULT_POLL_INTERVAL_SECONDS = 1.0

# DEFAULT_CAP_MS is this same bound in milliseconds, DERIVED from the seconds
# Monitor call is in MILLISECONDS. C4 imports DEFAULT_CAP_MS for the Monitor
# field and DEFAULT_CAP_SECONDS for the `--cap` it writes into the command line.
DEFAULT_CAP_SECONDS = 30 * 60
DEFAULT_CAP_MS = int(DEFAULT_CAP_SECONDS * 1000)


def _make_renderer(journal_path: str):
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
        help=(
            "Absolute path to the launching session transcript, taken "
            "verbatim from the hook's transcript_path — no path is "
            "reconstructed from parts. Required unless --reconcile is given."
        ),
    )
    parser.add_argument(
        "--journal",
        help=(
            "Absolute path to the run's journal.jsonl, derivable from "
            "transcriptDir+runId in the launch result. Required unless "
            "--reconcile is given."
        ),
    )
    parser.add_argument(
        "--task-id",
        help=(
            "The harness TASK id to match terminal records against (never "
            "the wf_ run id). Required unless --reconcile is given."
        ),
    )
    parser.add_argument(
        "--reconcile",
        metavar="RUN_DIR",
        help=(
            "Stamp a run nobody was watching, for a run whose watcher never "
            "ran or already exited: takes a run dir (the wf_* directory) or "
            "its journal.jsonl path verbatim, locates the launching "
            "transcript from that path's own location, and stamps the "
            "terminal record if one can be positively matched. Mutually "
            "exclusive with --transcript/--journal/--task-id; exits "
            "non-zero without writing anything on any ambiguity."
        ),
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help=(
            "Render each journal event line as it arrives, in addition to "
            "the terminal line. Default is silent-until-terminal: exactly "
            "one `terminal: <status>` line on stdout, and the exit-code "
            "contract is unchanged either way."
        ),
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
    follow: bool = False,
) -> int:
    watcher = TerminalWatcher(transcript_path, task_id)
    # No per-event Monitor invitation left to serve here (see the module's
    # own history: docs/plans/2026-09-26-coordinator-remedies-engine-items.md
    # C3, R2) -- the renderer is built ONLY under --follow. Left unbuilt in
    # the default case, not merely un-polled, so a caller reading `renderer`
    # after this line can tell "opted out" apart from "polled and had
    # nothing to say" without a follow flag threaded through every site.
    renderer = _make_renderer(journal_path) if follow else None
    deadline = time.monotonic() + cap_seconds

    while True:
        if renderer is not None:
            for line in renderer.poll():
                print(line)
                sys.stdout.flush()

        record = watcher.check_record()
        if record is not None:
            try:
                stamp_terminal(journal_path, task_id, record)
            except Exception:
                pass
            print(f"terminal: {record.status}")
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

    if args.reconcile is not None:
        return _reconcile_run(args.reconcile)

    if not (args.transcript and args.journal and args.task_id):
        print(
            "--transcript, --journal and --task-id are all required unless "
            "--reconcile is given",
            file=sys.stderr,
        )
        return 2

    return _watch(
        transcript_path=args.transcript,
        journal_path=args.journal,
        task_id=args.task_id,
        poll_interval=args.poll_interval,
        cap_seconds=args.cap,
        follow=args.follow,
    )


if __name__ == "__main__":
    sys.exit(main())

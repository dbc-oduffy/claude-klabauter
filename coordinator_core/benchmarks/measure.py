"""
coordinator_core.benchmarks.measure -- the cheap correct path for a
process-time citation (R5 of `docs/plans/2026-09-22-spawn-budget-and-census.md`).

Runnable as `python -m coordinator_core.benchmarks.measure [--k N] [--once]
[--json] -- <argv...>`. Prints ONE stamped line, the citation format a
budget figure in a row or record carries -- never a second clock, never a
platform branch of its own: it is a thin CLI over
`coordinator_core.benchmarks.process_time`'s existing primitives.

NEGATIVE SPEC: no `git` spawn of its own -- HEAD is read via
`coordinator_core.git.git_state.head_sha` (a no-spawn `.git`-file reader);
a platform that primitive cannot resolve on prints `head=unknown`, never a
`git rev-parse` fallback. On a platform `batched_process_time_ms`/
`single_invocation_tree_process_time` raise `NotImplementedError` on
(Linux for `--once`), this module prints the error and exits 2 -- it never
degrades to `time.process_time` or wall clock to paper over the gap.
"""

from __future__ import annotations

import argparse
import json
import sys

from coordinator_core.benchmarks.process_time import (
    batched_process_time_ms,
    single_invocation_tree_process_time,
)
from coordinator_core.git.git_state import head_sha


def _resolve_head() -> str:
    try:
        sha = head_sha(".")
    except Exception:
        return "unknown"
    if not sha:
        return "unknown"
    return sha[:12]


def _run(argv: list, k: int, once: bool) -> dict:
    if once:
        result = single_invocation_tree_process_time(argv)
        return {
            "process_time_ms": result["process_time_ms"],
            "procs_per_call": result["procs"],
            "instrument": "single_invocation_tree_process_time",
            "k": 1,
        }
    result = batched_process_time_ms(argv, k=k)
    return {
        "process_time_ms": result["process_time_ms"],
        "procs_per_call": result["procs_per_call"],
        "instrument": "batched_process_time_ms",
        "k": k,
    }


def main(argv: list = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv

    if "--" in raw_argv:
        split_idx = raw_argv.index("--")
        own_args = raw_argv[:split_idx]
        cmd = raw_argv[split_idx + 1 :]
    else:
        own_args = raw_argv
        cmd = []

    parser = argparse.ArgumentParser(
        prog="python -m coordinator_core.benchmarks.measure",
        description="Print one stamped process-time citation line for <argv...>.",
    )
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(own_args)

    if not cmd:
        parser.print_usage(sys.stderr)
        print("measure: an argv to measure is required after --", file=sys.stderr)
        return 2

    try:
        measured = _run(cmd, parsed.k, parsed.once)
    except NotImplementedError as exc:
        print(f"measure: NotImplementedError: {exc}", file=sys.stderr)
        return 2

    head = _resolve_head()
    stamped = {
        "process_time_ms": measured["process_time_ms"],
        "procs_per_call": measured["procs_per_call"],
        "instrument": measured["instrument"],
        "k": measured["k"],
        "platform": sys.platform,
        "warmth": "cold",
        "head": head,
    }

    if parsed.json:
        print(json.dumps(stamped))
    else:
        print(
            f"process_time_ms={stamped['process_time_ms']} "
            f"procs_per_call={stamped['procs_per_call']} "
            f"instrument={stamped['instrument']} "
            f"k={stamped['k']} "
            f"platform={stamped['platform']} "
            f"warmth={stamped['warmth']} "
            f"head={stamped['head']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

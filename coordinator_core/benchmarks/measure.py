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

NEGATIVE SPEC: a measured call that exits nonzero prints no figure (stdout
empty, `--json` included) and exits 1; an early exit is never a measurement.

NEGATIVE SPEC: a suite-shaped argv (per `check_test_suite_invocation.
classify_command`) is refused with exit 2 before anything is spawned; a tier
run is never a measurement. A scoped test file or `python -c` stays measurable.

CLONE MODE: `--clone-per-run [--k N] [--json] -- <argv containing {clone}>` mints a
fresh full clone of this repo's HEAD per run (`isolated_clone.fresh_git_clone`),
substitutes its path for `{clone}`, runs the argv with cwd=<clone> through
`single_invocation_tree_process_time`, and tears the clone down. It is for a job that
consumes its clone (a commit), so repeats on one clone would measure different work.
It refuses (exit 2, before any clone or child) an argv with no `{clone}` token, `--once`,
or a non-flag token resolving to the working repo outside the clone; it exits 1 with no
figure if a run fails or the working repo's HEAD moves.

NEGATIVE SPEC (clone mode): one `git clone` per run is spawned as setup OUTSIDE the
measured scope; the working HEAD is still read with no spawn.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import statistics
import sys
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
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
            "rc": result["rc"],
        }
    result = batched_process_time_ms(argv, k=k)
    return {
        "process_time_ms": result["process_time_ms"],
        "procs_per_call": result["procs_per_call"],
        "instrument": "batched_process_time_ms",
        "k": k,
        "rc": result["rc"],
    }


_CLONE_TOKEN = "{clone}"


def _path_within(path: str, root: str) -> bool:
    path, root = os.path.normcase(path), os.path.normcase(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _targets_working_repo(argv: list, clone: Path, source_root: Path) -> str:
    """Return the first non-flag token that names the working repo outside `clone`, else ''."""
    src = os.path.realpath(str(source_root))
    own = os.path.realpath(str(clone))
    for token in argv:
        if token.startswith("-"):
            token_value = token.partition("=")[2]
            if not token_value:
                continue
        else:
            token_value = token
        resolved = os.path.realpath(os.path.join(own, token_value))
        if resolved == src or (_path_within(resolved, src) and not _path_within(resolved, own)):
            return token
    return ""


def _substitute(argv: list, clone: Path) -> list:
    return [t.replace(_CLONE_TOKEN, str(clone)) for t in argv]


def _run_clone_per_run(cmd: list, k: int, source_root: Path):
    """What does one invocation of `cmd` cost on a fresh clone, `k` times.

    Returns `(runs, before, after)` where `after` is the working HEAD read after the last
    run, or stops early with that run's rc/HEAD when a run exits nonzero or HEAD moves.
    """
    from coordinator_core.benchmarks.isolated_clone import fresh_git_clone

    before = head_sha(source_root, fresh=True)
    runs = []
    after = before
    for _ in range(k):
        with fresh_git_clone(source_root, prefix="measure-", label="measure --clone-per-run") as clone:
            argv = _substitute(cmd, clone)
            leaked = _targets_working_repo(argv, clone, source_root)
            if leaked:
                raise _Refused(f"{leaked!r} names the working repo outside the clone")
            out_path = clone.parent / "stdout"
            result = single_invocation_tree_process_time(
                argv, cwd=str(clone), stdout_path=str(out_path)
            )
            stdout = out_path.read_bytes().decode("utf-8", errors="replace")
        runs.append(
            {
                "process_time_ms": result["process_time_ms"],
                "procs": result["procs"],
                "rc": result["rc"],
                "stdout": stdout,
            }
        )
        after = head_sha(source_root, fresh=True)
        if result["rc"] != 0 or after != before:
            break
    return runs, before, after


class _Refused(Exception):
    pass


def _main_clone_per_run(cmd: list, parsed, source_root) -> int:
    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[2]
    placeholder = root / "scratch" / "benchmark-clones" / "pending" / "clone"
    leaked = _targets_working_repo(_substitute(cmd, placeholder), placeholder, root)
    if leaked:
        print(f"measure: refused: {leaked!r} names the working repo outside the clone", file=sys.stderr)
        return 2
    try:
        runs, before, after = _run_clone_per_run(cmd, parsed.k, root)
    except _Refused as exc:
        print(f"measure: refused: {exc}", file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        print(f"measure: NotImplementedError: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"measure: refused: {exc}; no figure", file=sys.stderr)
        return 1

    if after != before:
        print(
            f"measure: refused: working repo HEAD moved during a run "
            f"(before={before} after={after}); no figure",
            file=sys.stderr,
        )
        return 1
    bad = next((r["rc"] for r in runs if r["rc"] != 0), 0)
    if bad:
        print(f"measure: refused: measured call exited rc={bad}; no figure", file=sys.stderr)
        return 1

    stamped = {
        "process_time_ms": statistics.median([r["process_time_ms"] for r in runs]),
        "procs_per_call": statistics.median([r["procs"] for r in runs]),
        "instrument": "single_invocation_tree_process_time",
        "k": len(runs),
        "platform": sys.platform,
        "warmth": "cold",
        "head": (before or "unknown")[:12],
    }
    if parsed.json:
        print(json.dumps({**stamped, "clone_per_run": True, "runs": runs}))
    else:
        print(" ".join(f"{key}={stamped[key]}" for key in stamped))
    return 0


def main(argv: list = None, *, source_root=None) -> int:
    declare_benchmark_origin()
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
    parser.add_argument("--clone-per-run", action="store_true")
    parsed = parser.parse_args(own_args)

    if not cmd:
        parser.print_usage(sys.stderr)
        print("measure: an argv to measure is required after --", file=sys.stderr)
        return 2

    if parsed.clone_per_run:
        if parsed.once:
            print("measure: refused: --once cannot combine with --clone-per-run", file=sys.stderr)
            return 2
        if not any(_CLONE_TOKEN in t for t in cmd):
            print(
                f"measure: refused: --clone-per-run needs a {_CLONE_TOKEN} token in the argv",
                file=sys.stderr,
            )
            return 2

    from coordinator_core.bash_guards.check_test_suite_invocation import classify_command

    suite = classify_command(shlex.join(cmd))
    if suite:
        print(
            f"measure: refused: {suite[0].detected} is a test-tier invocation; "
            "tier runs go through the suite route",
            file=sys.stderr,
        )
        return 2

    if parsed.clone_per_run:
        return _main_clone_per_run(cmd, parsed, source_root)

    try:
        measured = _run(cmd, parsed.k, parsed.once)
    except NotImplementedError as exc:
        print(f"measure: NotImplementedError: {exc}", file=sys.stderr)
        return 2

    rc = measured["rc"]
    if rc != 0:
        print(
            f"measure: refused: measured call exited rc={rc}; no figure",
            file=sys.stderr,
        )
        return 1

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

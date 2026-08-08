"""
coordinator_core.testing.full_runner — CLI entrypoint orchestrating C1
discovery (`collect.discover`) and C2 execution (`run.run_suites`).

Purpose: owns the DEC-1 exit contract (a directly-runnable Python CLI, never
routed through `cc_invoke`/`strangle_route` — no exit-masking green-wash) and
the DEC-10 fail-loud reporting: discover -> filter by --families -> for each
family named by --expect with zero matched files, emit a loud warning AND
mark the run failed (AC7); families NOT named by --expect that are empty get
a warning only and the run stays green (AC9) ->
run -> stream per-suite PASS/FAIL as suites complete -> emit a final
machine-scannable tally line. Process exit is 0 iff every dispatched suite
passed AND no `--expect`-named family was empty.

Port source: none — net-new (DR-059 harness authoring).
Spec backlink: docs/plans/2026-07-19-claude-klabauter-doe-full-test-runner.md § C3 (DEC-1, DEC-10)

Negative-spec:
    - Is NOT a shell polyglot trampoline (Finding 7) — a normal importable
      Python module invoked as `python3 -m coordinator_core.testing.full_runner`
      (DEC-11), with a plain `def main(argv) -> int` and a
      `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))` tail.
    - Does NOT silently green an `--expect`-named empty family (AC7) — an
      empty family named by `--expect` always flips the exit non-zero,
      regardless of whether every dispatched suite otherwise passed.
    - Does NOT fail on an empty family that `--expect` did not name (AC9) —
      `--repo .` with no `--expect` (claude-klabauter's own self-run, 3 of 4 families
      legitimately empty) stays green; this is the DEC-10 default warn-not-fail.
    - Does NOT swallow C2's fail-loud discipline (missing runner rc 127,
      timeout rc 124) — those are just failed suites folded into the same
      tally and exit contract as any other failure.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Sequence

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.testing.collect import ALL_FAMILIES, Suite, discover
from coordinator_core.testing import suite_mutex
from coordinator_core.testing.run import DEFAULT_TIMEOUT, SuiteResult, overall_ok, run_suites

logger = logging.getLogger(__name__)


def _git_toplevel(cwd: Path) -> Path:
    """Resolve the git toplevel of `cwd`, falling back to `cwd` itself if the
    lookup fails (e.g. not inside a git working tree)."""
    top = show_toplevel(str(cwd))
    if top:
        return Path(top)
    # Debug (not warning): running outside a git working tree is a
    # routine, expected case (see docstring's fallback contract), not a
    # fault worth surfacing on every normal invocation.
    logger.debug("full_runner: git toplevel lookup at %s failed, using cwd", cwd)
    return cwd


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m coordinator_core.testing.full_runner",
        description=(
            "Cross-repo full-test runner: discover -> run -> report, "
            "owning the DEC-1 exit contract."
        ),
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Repo root to discover+run suites in (default: git toplevel of cwd).",
    )

    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="Max parallel workers (default: half of os.cpu_count()).",
    )
    parser.add_argument(
        "--families",
        nargs="+",
        default=None,
        choices=sorted(ALL_FAMILIES),
        help="Restrict discovery to this subset of families (default: all four).",
    )
    parser.add_argument(
        "--expect",
        nargs="+",
        default=None,
        choices=sorted(ALL_FAMILIES) + ["all"],
        help=(
            "Family names (or 'all') that MUST have at least one discovered "
            "suite (DEC-10). Default unset: an empty family only warns, "
            "never fails the run."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Per-suite subprocess timeout in seconds (default {DEFAULT_TIMEOUT}).",
    )
    return parser


def _resolve_expect_families(expect: Sequence[str] | None) -> frozenset[str] | None:
    """Resolve `--expect` values to a concrete family set. `None` means unset
    (DEC-10 default: warn-not-fail on empty families). `["all"]` (or a
    literal `"all"` anywhere in the list) expands to every known family."""
    if expect is None:
        return None
    if any(name == "all" for name in expect):
        return frozenset(ALL_FAMILIES)
    return frozenset(expect)


def _families_present(suites: Sequence[Suite]) -> frozenset[str]:
    return frozenset(s.family for s in suites)


def _suite_label(result: SuiteResult) -> str:
    if result.suite is not None:
        return f"{result.suite.family}:{result.suite.path}"
    return "py-native (batched)"


def _stream_result(result: SuiteResult, stream) -> None:
    status = "PASS" if result.exit_code == 0 else "FAIL"
    print(f"[{status}] {_suite_label(result)} ({result.duration:.2f}s)", file=stream)


def main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root = Path(args.repo) if args.repo is not None else _git_toplevel(Path.cwd())

    requested_families = frozenset(args.families) if args.families else ALL_FAMILIES
    expect_families = _resolve_expect_families(args.expect)

    suites = discover(repo_root, families=requested_families)

    present_families = _families_present(suites)
    expect_failed = False

    # DEC-10: iterate every requested family (not just the --expect subset) so
    # an empty family outside --expect still gets its warn-only message (AC9);
    # only membership in expect_families decides fail-vs-warn per family.
    for family in sorted(requested_families):
        if family not in present_families:
            if expect_families is not None and family in expect_families:
                print(
                    f"[WARN] family '{family}' matched zero suites (expected non-empty)",
                    file=sys.stderr,
                )
                expect_failed = True
            else:
                print(
                    f"[WARN] family '{family}' matched zero suites",
                    file=sys.stderr,
                )

    # DR-088 layer 6 (R10, 2026-07-28). The mutex shipped and the guard's
    # deny leg consumed it correctly, but nothing ever CALLED acquire(), so
    # holder() always returned None and no suite run was ever serialized
    # against any other -- the module was live, the wiring was not. This is
    # the take side: full_runner is the one path in this repo that runs whole
    # suites concurrently against a shared tree, which is the exact harm
    # DR-088 was raised over (concurrent runs producing CORRUPTED results,
    # not merely slow ones).
    #
    # Waiting, not failing, is deliberate: a second runner blocked here is a
    # queued run, and aborting it would turn a resource control into a
    # correctness-irrelevant failure. ``suite_mutex.MUTEX_WAIT_SECS`` bounds
    # the wait so a stale-but-not-yet-reclaimable holder cannot wedge a run
    # forever; a timed-out acquire proceeds unserialized rather than
    # refusing, matching the fail-OPEN posture the guard's own mutex leg
    # takes.
    owner = suite_mutex.mutex_owner("full_runner")
    start = time.monotonic()
    with suite_mutex.held(owner, "full_runner", timeout=suite_mutex.MUTEX_WAIT_SECS) as acquired:
        if not acquired:
            current = suite_mutex.holder() or {}
            print(
                "[WARN] suite mutex held by %s for %ss — proceeding unserialized"
                % (current.get("owner", "<unknown>"), int(suite_mutex.MUTEX_WAIT_SECS)),
                file=sys.stderr,
            )
        results = run_suites(suites, repo_root=repo_root, timeout=args.timeout, jobs=args.jobs)
    duration = time.monotonic() - start

    for result in results:
        _stream_result(result, sys.stdout)

    passed = sum(1 for r in results if r.exit_code == 0)
    failed = len(results) - passed
    families_ran = len(present_families)
    print(
        f"{passed} passed, {failed} failed across {families_ran} families ({duration:.2f}s)"
    )

    if not overall_ok(results):
        return 1
    if expect_failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


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
    top = show_toplevel(str(cwd))
    if top:
        return Path(top)
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

    # DR-088 was raised over (concurrent runs producing CORRUPTED results,
    # correctness-irrelevant failure. ``suite_mutex.MUTEX_WAIT_SECS`` bounds
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

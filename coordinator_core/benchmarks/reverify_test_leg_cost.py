"""Driver measuring what a reverify test leg over a diff's selected targets costs, phase by phase.

Recomputes the target set with `reverify_scope.select_tests(repo, base, head)` and measures, each as
process time (this process's CPU plus descendants via `commit_leg_profile._ChildMeter`) and spawn count
(`subprocess.Popen` constructions); wall clock is never a figure:

  collect      `pytest --collect-only -q` over the set
  tests        the test run itself, under the test leg's budget
  base_export  `reverify_base_measure.measure` over the reds the test run found

    python coordinator_core/benchmarks/reverify_test_leg_cost.py [--base <rev>] [--head <rev>] [--repo <dir>]
        [--scratch <dir>] [--budget-s <s>] [--json]

Runs the real suite over the selected files: an EM-run measurement, never an executor's.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter
from coordinator_core.win_portability import no_console_creationflags

DEFAULT_BASE = "f0dbd760e2"
DEFAULT_HEAD = "81eafad88b"
_COUNT = re.compile(r"(\d+) (passed|failed|error|errors)\b")
_PHASES = ("collect", "tests", "base_export")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class _Bracket:
    """Process time and Popen count of everything run inside one `with` block."""

    def __init__(self) -> None:
        self.process_ms = 0.0
        self.spawns = 0

    def __enter__(self) -> "_Bracket":
        self._real_init = subprocess.Popen.__init__
        bracket = self

        def counting_init(proc, *a, **k) -> None:
            bracket.spawns += 1
            return bracket._real_init(proc, *a, **k)

        subprocess.Popen.__init__ = counting_init
        self._meter = _ChildMeter()
        self._meter.start()
        self._cpu0 = time.process_time()
        return self

    def __exit__(self, *exc) -> None:
        self.process_ms = round((time.process_time() - self._cpu0) * 1000.0 + self._meter.stop_ms(), 3)
        subprocess.Popen.__init__ = self._real_init


def _pytest(args: list, root: Path, timeout_s: float) -> tuple:
    """(exit code or None on timeout, stdout lines)."""
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args], cwd=str(root),
                              capture_output=True, text=True, errors="replace", timeout=timeout_s,
                              **no_console_creationflags())
    except subprocess.TimeoutExpired:
        return None, []
    return proc.returncode, proc.stdout.splitlines()


def _counts(lines: list) -> dict:
    for raw in reversed(lines):
        found = _COUNT.findall(raw)
        if found and " in " in raw:
            out: dict = {}
            for n, word in found:
                out[word.rstrip("s") if word.startswith("error") else word] = int(n)
            return out
    return {}


def measure(root: Path, base: str, head: str, scratch: Path, budget_s: float) -> dict:
    """The per-phase report for the reverify leg over `select_tests(root, base, head)`."""
    from coordinator_core.ops import reverify_base_measure
    from coordinator_core.ops.reverify_scope import select_tests
    from coordinator_core.ops.test_leg_run import _reds

    scratch.mkdir(parents=True, exist_ok=True)
    selection = select_tests(root, base, head)
    targets = list(selection["tests"])
    report: dict = {"base": base, "head": head, "targets": len(targets),
                    "tripwires": len(selection.get("tripwires", [])), "budget_s": budget_s}
    if not targets:
        report.update({f"{p}_ms": 0.0 for p in _PHASES}, **{f"{p}_spawns": 0 for p in _PHASES},
                      tests_run=0, red=0, tripwires_not_run=[], over_budget=False, dominant=None)
        return report

    with _Bracket() as collect:
        rc, lines = _pytest(["--collect-only", "-q", "--basetemp", str(scratch / "bt-collect"), *targets], root, budget_s)
    collected = sum(1 for ln in lines if "::" in ln)
    report.update(collect_ms=collect.process_ms, collect_spawns=collect.spawns, collect_exit=rc, collected=collected)

    with _Bracket() as tests:
        rc, lines = _pytest(["-rfE", "--basetemp", str(scratch / "bt-tests"), *targets], root, budget_s)
    over_budget = rc is None
    counts = _counts(lines)
    reds = list(_reds(lines))
    report.update(tests_ms=tests.process_ms, tests_spawns=tests.spawns, tests_exit=rc, over_budget=over_budget,
                  tests_run=counts.get("passed", 0) + counts.get("failed", 0), red=len(reds),
                  tripwires_not_run=list(selection.get("tripwires", [])) if over_budget else [])

    with _Bracket() as export:
        if reds:
            reverify_base_measure.measure(base, head, reds, str(root), budget_s)
    report.update(base_export_ms=export.process_ms, base_export_spawns=export.spawns)

    report["dominant"] = max(_PHASES, key=lambda p: report[f"{p}_ms"])
    return report


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="reverify_test_leg_cost",
        description=f"Per-phase process time and spawn count of the reverify test leg over select_tests(base, head); "
                    f"default selection base {DEFAULT_BASE}..{DEFAULT_HEAD}.",
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help="selection base (default %(default)s)")
    parser.add_argument("--head", default=DEFAULT_HEAD, help="selection head (default %(default)s)")
    parser.add_argument("--scratch", help="directory for pytest basetemp output (default: <repo>/scratch/reverify-test-leg-cost)")
    parser.add_argument("--repo", help="repo root to select and run in (default: this checkout)")
    parser.add_argument("--budget-s", type=float, help="per-phase timeout (default: the test leg's own budget)")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of field=value lines")
    args = parser.parse_args(argv)
    declare_benchmark_origin()
    budget_s = args.budget_s
    if budget_s is None:
        from coordinator_core.ops.test_leg_run import TEST_LEG_MINUTES_BUDGET
        budget_s = float(TEST_LEG_MINUTES_BUDGET * 60)
    root = Path(args.repo) if args.repo else _repo_root()
    scratch = Path(args.scratch) if args.scratch else root / "scratch" / "reverify-test-leg-cost"
    report = measure(root, args.base, args.head, scratch, budget_s)
    if args.json:
        print(json.dumps(report))
    else:
        for key, value in report.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

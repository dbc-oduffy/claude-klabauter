"""Spawn-count + process-time baseline for the `handoff.archive_transition`
supersede and chain paths — the named target any replacement must beat.

Why this exists: `op_budget_suspension.SUSPENDED_OPS` convicted
`handoff.archive_transition` at p50 250.0ms / max 828.1ms `process_ms` over
n=24, and that table's own text names this op as one of the three rows that
SPAWN — so `time.process_time()` in the measuring process read a FLOOR, not a
total, and the row carries no spawn count at all. That table forbids reusing
its figures as a reinstatement baseline without converting them to a spawn
count first. This module is that conversion, and it is the instrument the
rebuild proves itself against — not a re-measurement of the kill.

Each sample builds a THROWAWAY git repo and measures ONE invocation with
`process_time.single_invocation_tree_process_time`: root CPU plus every
descendant's, and a distinct-pid count that includes the root. The k-batched
primitive is wrong here by construction — the transition mutates the repo, so
the second invocation of a batch measures a different job than the first.

A `floor` mode measures the same CLI reaching `_import_module()` and then
refusing on a missing argument, so the interpreter-start + engine-import cost
that is not the transition's own work is attributed rather than charged to it.
It passes a bare subcommand, NEVER an empty argv: `archive-stamp-cli`'s
`main()` returns on `if not argv` BEFORE `_import_module()` runs, so an
empty-argv floor skips the engine import entirely and every `mode - floor`
subtraction then charges the whole of that import to the op.

`procs` is NOT the spawn count. It counts distinct pids in the job including
the root, and on Windows each `git` child arrives with a `conhost.exe`
alongside it (DR-373) — calibrated directly here, a python child spawning
0/1/2 `git --version` calls reports `procs` 1/3/5. One git spawn costs two job
processes, so the git count is `(procs - 1) / 2`. Reading `procs - 1` as the
spawn count doubles it.

`GIT_TRACE` is not the second opinion it looks like: `git_native._git` accepts
an `env=` that REPLACES the child environment wholesale, and the trace
variable does not survive that replacement — traced, supersede logs one git
invocation against two by job-object count. The job object cannot be evaded
this way.

The fixture is the SMALLEST input the op accepts — three handoffs, one commit
of history. Every figure here is therefore a lower bound on what the op costs
on a real corpus; a replacement that only just clears the bar against this
fixture has not cleared it in the fleet.

NEGATIVE SPEC: this module never reports wall clock as a result. `wall_ms` is
carried for context only — on a box running 50+ concurrent sessions it
measures peer load, and DR-344's brightline is process time and spawn count.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from coordinator_core.benchmarks.process_time import (  # noqa: E402
    single_invocation_tree_process_time,
)

CLI = REPO_ROOT / "coordinator" / "bin" / "archive-stamp-cli.py"

_PRED = """---
title: "baseline predecessor"
created: 2026-08-27
branch: "probe/baseline"
status: claimed
predecessor: none
category: infra
summary: baseline predecessor
deployment_state: in_flight
claimed_by: 00000000-0000-0000-0000-0000000000aa
claimed_at: '2026-08-27T00:00:00Z'
---

## Body

Fixture predecessor.
"""

_SUCC = """---
title: "baseline successor"
created: 2026-08-27
branch: "probe/baseline"
status: open
predecessor: state/handoffs/2026-08-27-probe-pred.md
category: infra
summary: baseline successor
deployment_state: in_flight
---

## Body

Fixture successor.
"""

_TERMINAL = """---
title: "baseline terminal"
created: 2026-08-27
branch: "probe/baseline"
status: closed
predecessor: none
category: infra
summary: baseline terminal
deployment_state: shipped
shipped_in: deadbeef
---

## Body

Fixture terminal baton — chain's git-mv precondition is a terminal
deployment_state, so a non-terminal fixture measures a refusal, not the move.
"""

ARGV = {
    # A bare subcommand, not `[]` — see the floor note in the module docstring.
    "floor": ["chain-archive-handoff"],
    "supersede": [
        "supersede-archive-handoff",
        "state/handoffs/2026-08-27-probe-pred.md",
        "--continued-into", "state/handoffs/2026-08-27-probe-succ.md",
        "--exclude", "state/handoffs/2026-08-27-probe-succ.md",
    ],
    "chain": [
        "chain-archive-handoff",
        "state/handoffs/2026-08-27-probe-terminal.md",
    ],
}


def _git(args, cwd: Path):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def build_fixture(root: Path) -> None:
    """Materialises the minimum corpus the op accepts.

    All four of `branch`, `predecessor`, `category`, `summary` are required —
    frontmatter validation refuses before the op is ever reached without them —
    and the predecessor needs `claimed_by`, or DR-242's claimed-or-shipped gate
    refuses first.
    """
    hd = root / "state" / "handoffs"
    hd.mkdir(parents=True)
    (hd / "2026-08-27-probe-pred.md").write_text(_PRED, encoding="utf-8")
    (hd / "2026-08-27-probe-succ.md").write_text(_SUCC, encoding="utf-8")
    (hd / "2026-08-27-probe-terminal.md").write_text(_TERMINAL, encoding="utf-8")
    _git(["init", "-q", "-b", "probe/baseline"], root)
    _git(["config", "user.email", "probe@example.invalid"], root)
    _git(["config", "user.name", "probe"], root)
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "fixture"], root)


def sample(mode: str, out_dir: Path, idx: int) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"handoff-baseline-{mode}-"))
    root = tmp / "repo"
    root.mkdir()
    try:
        build_fixture(root)
        # `cwd` IS the repo-root isolation — `archive_stamp._resolve_repo_root_for`
        # resolves off it. No env var participates; do not add one back.
        res = single_invocation_tree_process_time(
            [sys.executable, str(CLI), *ARGV[mode]],
            env=dict(os.environ),
            cwd=str(root),
            stdout_path=str(out_dir / f"{mode}-{idx}.out"),
            stderr_path=str(out_dir / f"{mode}-{idx}.err"),
        )
        res["archived"] = sorted(
            p.relative_to(root).as_posix() for p in root.glob("archive/handoffs/**/*.md")
        )
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def measure(n: int, out_dir: Path) -> dict:
    report: dict = {}
    for mode in ("floor", "supersede", "chain"):
        rows = [sample(mode, out_dir, i) for i in range(n)]
        report[mode] = {
            "n": n,
            "process_ms_p50": statistics.median(r["process_time_ms"] for r in rows),
            "process_ms_max": max(r["process_time_ms"] for r in rows),
            "procs_p50": statistics.median(r["procs"] for r in rows),
            "procs_max": max(r["procs"] for r in rows),
            "wall_ms_p50": statistics.median(r["wall_ms"] for r in rows),
            "rcs": sorted({r["rc"] for r in rows}),
            "archived_last": rows[-1]["archived"],
        }
    return report


def check(report: dict) -> list:
    """Refuse a baseline built from invocations that did not do the job.

    Without this the recorded numbers time whatever happened. A fixture
    regression, changed `--exclude` semantics, or the CLI's own
    `_TRANSPORT_FAIL` on an unresolvable engine root each produce a full,
    plausible-looking table — and these figures are cited as the target a
    rebuild must beat. `rc` and `archived` were already collected here;
    nothing read them.
    """
    problems = []
    for mode in ("supersede", "chain"):
        rcs = report[mode]["rcs"]
        if rcs != [0]:
            problems.append(f"{mode}: expected every sample rc 0, got {rcs}")
        if not report[mode]["archived_last"]:
            problems.append(f"{mode}: archived nothing — the fixture did not exercise the path")
    if report["floor"]["archived_last"]:
        problems.append(
            f"floor: archived {report['floor']['archived_last']} — the floor mode did work"
        )
    return problems


def main(argv: list[str]) -> int:
    declare_benchmark_origin()
    n = int(argv[1]) if len(argv) > 1 else 15
    out_dir = Path(tempfile.mkdtemp(prefix="handoff-baseline-out-"))
    report = measure(n, out_dir)
    problems = check(report)
    if problems:
        # Per-sample stdout/stderr is the only evidence of WHY a sample failed,
        # so it survives exactly the run that needs it.
        print(f"BASELINE REFUSED — per-sample stdio kept at {out_dir}", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 1
    shutil.rmtree(out_dir, ignore_errors=True)
    print(json.dumps(report, indent=2))
    for mode, row in report.items():
        print(
            f"{mode:10s} n={row['n']} "
            f"process p50={row['process_ms_p50']:.1f}ms max={row['process_ms_max']:.1f}ms "
            f"procs={row['procs_max']} rcs={row['rcs']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

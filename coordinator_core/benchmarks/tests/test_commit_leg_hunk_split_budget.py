"""Standing gate: a hunk-split landing (`hunk_stage.split` then `commit_paths` with the staged blob)
on a fresh isolated clone costs at most 150ms of process time and spawns nothing.

DERIVATION. Same bar as `test_commit_leg_process_budget.py` (DR-368: 290ms op target less the
134ms cold-invoke floor, rounded down to 150ms); `_BUDGET_MS` is never raised here. The leg is
the `time.process_time` bracket of split + commit in the measuring process; `spawns` counts
`subprocess.Popen` constructions in the bracket, so children cost nothing when it is 0.

SHAPE. Per run: one fresh clone, one tracked 4000-line file (every eighth line blank, as in source,
so the matcher sees repeated lines and a changed middle spanning the file) that two runs both
dirtied (run A at line 5 before run B's stage, run B at line 3995), signing off. The measured call
lands B's hunk only, after one warm-up `commit_paths` (the profile's second-commit-in-process shape:
store hygiene is stubbed out: it fires a detached daemon that is not the leg and would outlive the clone). `_RUNS_PER_ARM` runs; the median is asserted because a commit consumes its clone.
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.isolated_clone import fresh_git_clone

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BUDGET_MS = 150.0
_RUNS_PER_ARM = 3
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_DRIVER = r"""
import json, subprocess, sys, time
from pathlib import Path

import shutil

clone = Path(sys.argv[1])
_MODULE = Path("coordinator_core/ops/dispatch_emit/hunk_stage.py")
shutil.copyfile(Path(sys.argv[2]) / _MODULE, clone / _MODULE)
sys.path.insert(0, str(clone))
from coordinator_core.benchmarks import declare_benchmark_origin
declare_benchmark_origin()
from coordinator_core.git.run import run_git

REL = "hunk_split_bench_subject.py"
spawns = []
original = subprocess.Popen.__init__


def _counting(self, args, *a, **k):
    spawns.append(args if isinstance(args, str) else list(args))
    return original(self, args, *a, **k)


def git(*args):
    res = run_git(["-C", str(clone), *args])
    if res.returncode != 0:
        raise RuntimeError(f"git {args} rc={res.returncode}: {res.stderr}")
    return res.stdout.strip()


def rows(edits):
    out = ["" if i % 8 == 0 else f"x_{i} = {i}" for i in range(1, 4001)]
    for n, tag in edits.items():
        out[n - 1] = f"x_{n} = '{tag}'"
    return "\n".join(out) + "\n"


git("config", "commit.gpgsign", "false")
git("config", "user.email", "bench@example.com")
git("config", "user.name", "bench")
path = clone / REL
path.write_text(rows({}), encoding="utf-8", newline="\n")
git("add", REL)
git("commit", "-q", "-m", "bench seed")

from coordinator_core.git.commit import commit_paths
from coordinator_core.ops.dispatch_emit import hunk_stage
from coordinator_core.ops.dispatch_emit import write_baseline as wb

from coordinator_core.git import store_hygiene
store_hygiene.maybe_refresh = lambda *a, **k: False
warm = clone / "hunk_split_bench_warmup.txt"
warm.write_text("warm\n", encoding="utf-8", newline="\n")
commit_paths(clone, ["hunk_split_bench_warmup.txt"], "bench warmup")

base_a = wb.capture(clone, [REL])[REL]
path.write_text(rows({5: "A-EDIT"}), encoding="utf-8", newline="\n")
base_b = wb.capture(clone, [REL])[REL]
path.write_text(rows({5: "A-EDIT", 3995: "B-EDIT"}), encoding="utf-8", newline="\n")

subprocess.Popen.__init__ = _counting
t0 = time.process_time()
got = hunk_stage.split(
    clone, REL, own_run_id="run-b", own_baseline_blob=base_b, foreign=[("run-a", base_a)]
)
outcome = None
if got.staged_blob is not None:
    outcome = commit_paths(clone, [REL], "bench", supplied_blobs={REL: got.staged_blob})
leg_ms = (time.process_time() - t0) * 1000.0
subprocess.Popen.__init__ = original

print(json.dumps({
    "committed": outcome is not None and bool(outcome.sha),
    "overlaps": [list(o) for o in got.overlaps],
    "leg_ms": round(leg_ms, 3),
    "spawns": len(spawns),
    "spawned": spawns[:3],
}))
"""


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _run_arm() -> list:
    source = _source_root()
    reports = []
    for i in range(_RUNS_PER_ARM):
        with fresh_git_clone(source, prefix="commit-leg-hunk-split-", label=f"hunk-split-{i}") as clone:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", _DRIVER, str(clone), str(source)],
                cwd=str(clone), capture_output=True, text=True, timeout=300,
                creationflags=_NO_WINDOW,
            )
            assert proc.returncode == 0, proc.stderr[-2000:]
            report = json.loads(proc.stdout.strip().splitlines()[-1])
            assert report["committed"] is True, report
            reports.append(report)
    return reports


def test_hunk_split_landing_is_within_budget_and_spawns_nothing():
    reports = _run_arm()
    legs = [r["leg_ms"] for r in reports]
    assert [r["spawns"] for r in reports] == [0] * len(reports), [r["spawned"] for r in reports]
    assert statistics.median(legs) <= _BUDGET_MS, (
        f"hunk-split commit leg median {statistics.median(legs):.1f}ms > {_BUDGET_MS}ms; legs={legs}"
    )

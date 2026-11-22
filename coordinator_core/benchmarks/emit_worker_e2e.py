"""Worker-e2e falsifier: the default `emit-dispatch-workflow.py --plan` invocation on three axes.

Prints one JSON object {process_ms, spawns, lines, path, ...}. Run at the run base and again at close; the
close reading is held against `BUDGET_MS`, `spawns` not above base, and `lines` below `LINES_BASE`.

AXES. `process_ms` is tree process time (job object / kqueue / fork accounting) from
`batched_process_time_ms`, min over SAMPLES of K_INVOCATIONS-call batches: the same reading
`test_emit_dispatch_process_budget.py` gates, never wall clock. `spawns` is `procs_per_call` from that
same best sample (on Windows a git child costs two: the child plus its console host). `lines` is the
`wc -l` total of `LINE_FILES`: the emit modules and the three door sources a plan row writes.

PATH. The reading is a cold interpreter: `sys.executable <CLI>` pays interpreter start, imports and the
emit with nothing resident. An in-process `lru_cache` pays only on the warm-engine trampoline path, which
this harness does not drive; `path` names which one the figure is, so a cache edit is read against it.

Run: `python coordinator_core/benchmarks/emit_worker_e2e.py [--samples N] [--k N]`.
Scratch is a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home  # noqa: E402
from coordinator_core.benchmarks.process_time import batched_process_time_ms  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

K_INVOCATIONS = 10
SAMPLES = 5
BUDGET_MS = 500.0
#: `door.c + door_posix.c + door_core.c` at the run base (plan census).
LINES_BASE = 7315
PATH_COLD = "cold-interpreter"

_CLI = _REPO_ROOT / "coordinator" / "bin" / "emit-dispatch-workflow.py"
_FIXTURE_PLAN = _REPO_ROOT / "coordinator_core" / "benchmarks" / "fixtures" / "emit_full_path_plan.md"
_PLAN_REL = "docs/plans/emit-full-path-plan.md"
_DOOR = "coordinator_core/warm/door"
_EMIT = "coordinator_core/ops/dispatch_emit"
#: Files the emit-memoise plan writes whose size is the line axis (door sources count the 7315 total).
LINE_FILES = (
    f"{_EMIT}/emit.py",
    f"{_EMIT}/ask_contract.py",
    f"{_EMIT}/ask_staged_prompts.py",
    f"{_EMIT}/ask_stage.py",
    f"{_DOOR}/door.c",
    f"{_DOOR}/door_posix.c",
    f"{_DOOR}/door_core.c",
)
_DOOR_FILES = LINE_FILES[-3:]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _build_repo(root: Path) -> Path:
    repo = root / "repo"
    plan = repo / _PLAN_REL
    plan.parent.mkdir(parents=True)
    shutil.copyfile(_FIXTURE_PLAN, plan)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "budget@example.invalid")
    _git(repo, "config", "user.name", "budget")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def count_lines(paths=LINE_FILES, repo_root: Path = _REPO_ROOT) -> dict[str, int]:
    """`wc -l` (newline count) per repo-relative path; a missing file counts 0."""
    out = {}
    for rel in paths:
        p = repo_root / rel
        out[rel] = p.read_bytes().count(b"\n") if p.is_file() else 0
    return out


def measure(samples: int = SAMPLES, k: int = K_INVOCATIONS) -> dict:
    """One reading of the default `--plan` emit: process_ms, spawns, lines, path; rc must be 0 and the script written."""
    tmp = Path(tempfile.mkdtemp(prefix="emit_worker_e2e_"))
    try:
        repo = _build_repo(tmp)
        out = repo / "out" / "budget.workflow.mjs"
        out.parent.mkdir()
        cmd = [
            sys.executable, str(_CLI),
            "--plan", str(repo / _PLAN_REL),
            "--out", str(out),
            "--force",
            "--repo-root", str(repo),
        ]
        env = {**os.environ, "COORDINATOR_WORKFLOW_ADMISSION_DISABLE": "1"}
        with fixture_settings_home(tmp):
            env["COORDINATOR_SETTINGS_HOME"] = os.environ["COORDINATOR_SETTINGS_HOME"]
            readings = [batched_process_time_ms(cmd, k=k, env=env) for _ in range(samples)]
        best = min(readings, key=lambda r: r["process_time_ms"])
        if best["rc"] != 0 or not out.is_file():
            raise RuntimeError(f"emit did not succeed (rc={best['rc']}, script written={out.is_file()})")
        lines = count_lines()
        return {
            "process_ms": round(best["process_time_ms"], 1),
            "spawns": best["procs_per_call"],
            "lines": sum(lines.values()),
            "door_lines": sum(lines[f] for f in _DOOR_FILES),
            "door_lines_base": LINES_BASE,
            "line_detail": lines,
            "path": PATH_COLD,
            "k": k,
            "samples": samples,
            "sample_process_ms": sorted(round(r["process_time_ms"], 1) for r in readings),
            "budget_ms": BUDGET_MS,
            "under_budget": best["process_time_ms"] < BUDGET_MS,
            "platform": sys.platform,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=SAMPLES)
    ap.add_argument("--k", type=int, default=K_INVOCATIONS)
    args = ap.parse_args(argv)
    print(json.dumps(measure(args.samples, args.k), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

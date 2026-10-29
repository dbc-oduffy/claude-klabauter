"""Resume-emit benchmark: `ask_gate.gate` on a halted `review_blocked` run, in-process, warm.

Builds a tmp git repo (a sizing with a `plan:` edge, a plan whose spine has landed and open rows,
a halted `review_blocked` run record, and `--commits` first-parent commits after the plan), drives
`gate(...)` (the engine-ask submit hook's resume branch: `resumable_plan` ->
`resume_landed_state` + `rows_landed_outside_executor`) and reports process ms (cold and warm), git
spawns, `commit_walk.commit_meta` calls, the verdict, and a cProfile top-N of one profiled call.
`measure()` is the single entry the cost pin (`test_submit_hook_resume_emit_cost_pin.py`) drives.

Run: `python -m coordinator_core.benchmarks.submit_hook_resume_emit [--commits N] [--top T]
[--profile-out PATH] [--enforce-bar]`. Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.win_portability import no_console_creationflags

#: The brightline process-time bar for the hook emit (CLAUDE.md § The brightline).
BAR_MS = 500.0
SIZING_REL = "state/sizings/2026-10-29-resume-bench.yaml"
PLAN_REL = "docs/plans/2026-10-29-resume-bench.md"
RUN_ID = "warp-2026-10-29-resume-bench-20261029T000000-aaaaaa"
#: Spine rows: A1 lands by a checkpoint commit; the rest stay open and are what the resume stages.
OPEN_ROWS = ("A2", "A3", "A4")
DEFAULT_COMMITS = 48
_SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _fast_import_stream(commits: int) -> bytes:
    """A `git fast-import` stream: seed, the plan, the A1 checkpoint, then churn commits that never
    touch an open row's declared write, so the outside-landing walk runs down to the plan's creation."""
    out: list[bytes] = []
    epoch = 1_700_000_000
    mark = 0

    def commit(message: str, files: dict[str, str]) -> None:
        nonlocal epoch, mark
        mark += 1
        epoch += 60
        msg = message.encode("utf-8")
        out.append(f"commit refs/heads/main\nmark :{mark}\ncommitter t <t@t.example> {epoch} +0000\n".encode())
        out.append(f"data {len(msg)}\n".encode() + msg + b"\n")
        if mark > 1:
            out.append(f"from :{mark - 1}\n".encode())
        for path, text in files.items():
            body = text.encode("utf-8")
            out.append(f"M 100644 inline {path}\ndata {len(body)}\n".encode() + body + b"\n")

    commit("seed", {"README.md": "seed\n"})
    commit("plan", {PLAN_REL: _plan_text()})
    commit(
        f"checkpoint(wave 1): 1 row - A1\n\nCheckpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234",
        {"src/a1.py": "a1\n"},
    )
    for n in range(commits):
        commit(
            f"churn {n}",
            {f"src/churn/m{n % 7}.py": f"{n}\n", f"docs/notes/n{n % 5}.md": f"{n}\n", f"pkg/p{n}.py": f"{n}\n"},
        )
    return b"".join(out)


def _plan_text() -> str:
    rows = [("A1", ["src/a1.py"])] + [(i, [f"src/{i.lower()}.py"]) for i in OPEN_ROWS]
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  writes:\n" + "".join(f"    - {w}\n" for w in writes)
        for i, writes in rows
    )
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _git(root: Path, *args: str, stdin: bytes | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t.example", *args],
        input=stdin, check=True, capture_output=True, **no_console_creationflags(),
    )


def build_fixture(root: Path, commits: int = DEFAULT_COMMITS) -> None:
    """Populate `root` (an empty dir): a packed git history, the sizing, and a halted run record."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker

    _git(root, "init", "-q", "-b", "main")
    _git(root, "fast-import", "--quiet", stdin=_fast_import_stream(commits))
    _git(root, "reset", "-q", "--hard", "main")
    _git(root, "repack", "-adq")
    sizing = {
        "schema": "sizing-object", "name": "resume bench", "intent": "exercise the resume emit",
        "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized", "plan": PLAN_REL,
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-resume-bench-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    (root / SIZING_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / SIZING_REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8", newline="\n")
    rl.start(root, RUN_ID)
    rl.bind(root, RUN_ID, SIZING_REL, ask_text="the ask")
    write_halt_marker(
        record_dir(root, RUN_ID), halted="review_blocked", sizing_rel=SIZING_REL, plan_rel=PLAN_REL,
    )


def _top(stats: pstats.Stats, n: int) -> str:
    stats.sort_stats("cumulative").print_stats(n)
    return stats.stream.getvalue()


def measure(commits: int = DEFAULT_COMMITS, top: int = 20) -> dict:
    """Build the fixture and drive `gate` three ways: cold, warm (counted), and profiled.

    Returns `warm_process_ms`, `cold_process_ms`, `git_spawns` and `commit_meta_calls` for one warm
    call, the verdict (`resumes_halted_run`, `resume_rows`), and `top` (cProfile text)."""
    from coordinator_core.git import commit_walk
    from coordinator_core.ops.dispatch_emit.ask_gate import gate

    mp = pytest.MonkeyPatch()
    scratch = Path(tempfile.mkdtemp(prefix="resume-emit-bench-", dir=_scratch_parent()))
    try:
        root = scratch / "repo"
        root.mkdir()
        build_fixture(root, commits)
        mp.chdir(root)
        mp.setenv("COORDINATOR_SESSION_ID", _SESSION_ID)
        mp.delenv("DELIVERABLE_ID", raising=False)

        def call():
            return gate(root, SIZING_REL)

        t0 = time.process_time()
        verdict = call()
        cold_ms = (time.process_time() - t0) * 1000.0
        meta_calls = [0]
        real_meta = commit_walk.commit_meta

        def counted(*a, **k):
            meta_calls[0] += 1
            return real_meta(*a, **k)

        mp.setattr(commit_walk, "commit_meta", counted)
        with _count_spawns_attributed(mp) as spawns:
            warm = call()
        spawn_argv = [s.argv for s in spawns]
        profiler = cProfile.Profile()
        profiler.runcall(call)
        mp.setattr(commit_walk, "commit_meta", real_meta)
        try:
            warm_ms = in_process_time_ms(call)["process_time_ms"]
        except ValueError:
            warm_ms = None
        text = _top(pstats.Stats(profiler, stream=io.StringIO()), top)
        return {
            "commits": commits,
            "cold_process_ms": round(cold_ms, 1),
            "warm_process_ms": warm_ms,
            "git_spawns": len(spawn_argv),
            "git_spawn_argv": spawn_argv,
            "commit_meta_calls": meta_calls[0],
            "halt": warm.halt,
            "resume_plan": warm.resume_plan,
            "resume_rows": list(warm.resume_rows or []),
            "resumes_halted_run": bool(
                verdict.halt is None and warm.halt is None and warm.resume_plan == PLAN_REL
                and list(warm.resume_rows or []) == list(OPEN_ROWS)
            ),
            "top": text,
        }
    finally:
        mp.undo()
        rmtree_or_raise(scratch, label="resume-emit-bench")


def _scratch_parent() -> str:
    from coordinator_core.temp_layout import managed_tempdir

    return str(managed_tempdir())


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commits", type=int, default=DEFAULT_COMMITS)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--profile-out", default=None, help="write the cProfile top-N here")
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when warm process ms >= {BAR_MS:.0f}")
    args = ap.parse_args()
    result = measure(args.commits, args.top)
    top = result.pop("top")
    for key, value in result.items():
        print(f"{key}: {value}")
    over = result["warm_process_ms"] is None or result["warm_process_ms"] >= BAR_MS
    print(f"bar: {'OVER' if over else 'UNDER'} {BAR_MS:.0f}ms process time warm")
    print(top)
    if args.profile_out:
        Path(args.profile_out).write_text(top, encoding="utf-8", newline="\n")
    if not result["resumes_halted_run"]:
        return 1
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())

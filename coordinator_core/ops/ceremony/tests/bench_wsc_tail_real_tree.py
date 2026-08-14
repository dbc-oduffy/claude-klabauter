"""Baseline measurement harness for `ceremony.wsc_tail`, driven against a
real-shape (deliberately dirty) local clone of claude-klabauter -- see
docs/plans/2026-07-23-wsc-tail-slim-down.md C1.

Not a pytest test (deliberately outside the `test_*.py` convention so
`python3 -m pytest coordinator_core/` never collects it) -- a standalone,
re-runnable measurement CLI, separate from the `coordinator_core/benchmarks/`
fixture-KPI framework on purpose: that framework's fixture corpus is small
and synthetic, which is exactly the wrong oracle for this measurement (the
plan's whole point -- `dirty_tree_gate`'s whole-worktree scan and
`commit.anchors`' staged-diff read scale with REAL corpus size, and a tiny
fixture undermeasures both). This script drives the real `_handler` against
a full local clone of the actual repo instead.

Reads the per-step timing map wsc_tail.py's `_TailTiming` writes into the
ceremony receipt's `wsc-tail-timing` D-node (non-tail, added 2026-07-23
commit 01ca868e) after each real `_handler` pass, and reports median-per-step
across N passes plus KEEP-set vs SHED-set subtotals against the plan's <2s
budget.

USAGE (naked Python 3.11+, no bash) -- NEVER point this at the live working
tree; it really commits, really writes files, and (with the default
deferred-push spawn monkeypatched off below) never contacts a remote, but a
repo any other session is using is not safe input:

    1. Make an isolated local copy of the repo (NOT the live tree), e.g.:
           cp -R /path/to/claude-klabauter /path/to/sandbox-clone
    2. Remove its remote(s) (belt-and-suspenders -- this script also refuses
       to run against a repo with a remote configured):
           cd /path/to/sandbox-clone && git remote remove origin
    3. Run:
           python3 coordinator_core/ops/ceremony/tests/bench_wsc_tail_real_tree.py \\
               /path/to/sandbox-clone --passes 5
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


# Tracked files to (re-)dirty each pass -- a spread across the three dirs the
# plan's real-tree complaint names, chosen as ordinary text/source files that
# tolerate an appended marker line without breaking import/collection.
_TRACKED_TOUCH = [
    "coordinator_core/ops/ceremony/tail_ops.py",
    "coordinator_core/ops/ceremony/commit_gates.py",
    "coordinator_core/ops/ceremony/commit_pipeline.py",
    "coordinator_core/distill/_common.py",
    "coordinator_core/ops/ceremony/receipt_emit.py",
    "state/cruft-sweep-log.md",
    "state/doctor-last-run.json",
    "docs/README.md",
    "docs/install/agent-install-manifest.json",
]

# Untracked new files, same spread -- overwritten (not appended) each pass so
# the corpus shape stays constant across passes.
_UNTRACKED_NEW = [
    "coordinator_core/ops/ceremony/tests/_baseline_scratch_a.py",
    "coordinator_core/scratch_baseline_note.txt",
    "state/scratch/_baseline_scratch_state.md",
    "state/debt-backlog/_baseline_scratch_debt.yaml",
    "docs/wiki/_baseline_scratch_wiki.md",
]

_MARKER = "# baseline-dirty-marker (wsc-tail-slim-down C1 measurement)\n"


def _dirty_tree(root: Path) -> tuple[int, int]:
    """Append a marker line to each tracked file (stays modified-but-
    uncommitted across passes, since these paths are never in a pass's own
    `stage_paths`) and (re-)write each untracked file. Returns (touched,
    created) counts for the caller to report per pass."""
    touched = 0
    for rel in _TRACKED_TOUCH:
        p = root / rel
        if not p.is_file():
            continue
        with p.open("a", encoding="utf-8") as fh:
            fh.write(_MARKER)
        touched += 1
    created = 0
    for rel in _UNTRACKED_NEW:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_MARKER, encoding="utf-8")
        created += 1
    return touched, created


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _run_one_pass(root: Path, common_dir: Path, wsc_tail_mod: Any, pass_no: int) -> dict[str, Any]:
    """One real `ceremony.wsc_tail._handler` invocation: a fresh sid, a
    fresh throwaway staged file (so there is always something new to commit
    even though the dirtied tracked/untracked files above are deliberately
    never staged), timed end-to-end, with the per-step timing map read back
    from the emitted receipt."""
    sid = f"bench-{uuid.uuid4().hex[:12]}"
    bench_file = root / "tasks" / "bench" / f"note-{sid}.md"
    bench_file.parent.mkdir(parents=True, exist_ok=True)
    bench_file.write_text(f"# baseline bench pass {pass_no}\nsid={sid}\n", encoding="utf-8")
    rel = f"tasks/bench/note-{sid}.md"

    params = {
        "sid": sid,
        "subject": f"workstream-complete: wsc-tail baseline bench pass {pass_no}",
        "stage_paths": [rel],
        "caller_paths": [rel],
    }

    start = time.perf_counter()
    result = asyncio.run(wsc_tail_mod._handler(params, repo_root=common_dir))
    elapsed = time.perf_counter() - start

    if result.get("exit_code") not in (0, 2):
        print(
            f"WARNING pass {pass_no}: exit_code={result.get('exit_code')} error={result.get('error')}",
            file=sys.stderr,
        )
        return {"elapsed": elapsed, "steps": {}, "result": result}

    # Read from the wire response's own `timing` key, not the persisted
    # receipt -- the receipt's D-node is built before `receipt_emit` and
    # `sentinel_clear` are measured (see wsc_tail.py's own comment at the
    # D-node build site), so it is missing those two rows. `result["timing"]`
    # is the full `timing.as_list()` snapshot taken at return time and
    # carries all steps. (Review: staff-eng F3/F4 -- prior code read the
    # receipt and silently dropped `receipt_emit`/`sentinel_clear`.)
    steps: dict[str, float] = {
        entry["step"]: entry["ms"] for entry in result.get("timing", [])
    }
    return {"elapsed": elapsed, "steps": steps, "result": result}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo_root", help="Path to the isolated sandbox clone's worktree root")
    ap.add_argument("--passes", type=int, default=5)
    args = ap.parse_args()

    root = Path(args.repo_root).resolve()
    common_dir = (root / ".git").resolve()
    if not common_dir.is_dir():
        print(f"error: {common_dir} is not a directory -- is {root} a git worktree?", file=sys.stderr)
        return 1

    # Refuse to run against anything that still has a real remote configured
    # -- belt-and-suspenders against ever contacting a network origin from
    # this measurement harness (the deferred-push spawn is monkeypatched out
    # below regardless, but this is a second independent guard).
    remotes = _git(["remote"], root).stdout.strip()
    if remotes:
        print(
            f"error: refusing to run -- {root} still has remote(s) configured: {remotes!r}. "
            "Remove them first (git remote remove <name>).",
            file=sys.stderr,
        )
        return 1

    sys.path.insert(0, str(root))
    import coordinator_core.ops.ceremony.wsc_tail as wsc_tail_mod  # noqa: E402

    # The deferred-push spawn is fire-and-forget and not part of the
    # blocking-path KEEP-set cost story (see plan's KEEP-set table) --
    # monkeypatched off exactly as the existing KPI test
    # (test_kpi_wsc_tail_blocking_path_under_2s) does, so a real detached
    # child process is never spawned by this measurement.
    wsc_tail_mod._spawn_deferred_push_skip_loud = lambda wt: None

    per_pass: list[dict[str, Any]] = []
    for i in range(1, args.passes + 1):
        touched, created = _dirty_tree(root)
        pass_result = _run_one_pass(root, common_dir, wsc_tail_mod, i)
        per_pass.append(pass_result)
        n_steps = len(pass_result["steps"])
        print(
            f"pass {i}: touched={touched} created={created} elapsed={pass_result['elapsed']*1000:.1f}ms "
            f"steps_captured={n_steps} exit_code={pass_result['result'].get('exit_code')}",
            file=sys.stderr,
        )

    all_step_names: list[str] = []
    for p in per_pass:
        for name in p["steps"]:
            if name not in all_step_names:
                all_step_names.append(name)

    medians: dict[str, float] = {}
    for name in all_step_names:
        vals = [p["steps"][name] for p in per_pass if name in p["steps"]]
        medians[name] = statistics.median(vals) if vals else float("nan")

    total_elapsed_median = statistics.median([p["elapsed"] for p in per_pass])

    print(f"\n=== Per-step median wall-ms (n={args.passes} passes) ===")
    print(f"{'step':<40} {'median_ms':>12} {'samples':>8}")
    for name in all_step_names:
        vals = [p["steps"][name] for p in per_pass if name in p["steps"]]
        print(f"{name:<40} {medians[name]:>12.2f} {len(vals):>8}")

    # `precommit_tail_total` is a WRAPPER span around FOUR `precommit.*`
    # sub-spans (see wsc_tail.py's `_run_precommit_tail` docstring) -- summing
    # every row double-counts that nesting. Updated 2026-07-23 C9: the C2
    # archive-sweeps span moved OUT from under this wrapper and off the
    # blocking path entirely (renamed `postcommit.archive_sweeps_detached`,
    # fired post-commit -- it is a TOP-LEVEL step now, not nested here); the
    # C4-Phase-2 completion-entry scaffold (and its `precommit.completion_
    # entry` span) was removed from this op outright, DoE's SKILL.md now owns
    # that write; and C6 split the single `precommit.coverage_gate` span into
    # separately-timed fire/join halves. The additive top-level sequence is:
    # params_and_push_mode, step1_resolve, sentinel_read, precommit_tail_total
    # (subsuming the four precommit.* rows below), trailer_derivation,
    # sentinel_write, commit_pipeline, stamp_and_ship, origin_stub_close,
    # postcommit.archive_sweeps_detached, deferred_push_spawn,
    # cs_release_artifact, receipt_emit, sentinel_clear. `cs_archive` was
    # REMOVED from this op (`e510140a`) -- archival is now solely a
    # SessionEnd occasion, not a `wsc_tail` tail step; this bench no longer
    # measures it.
    _NESTED_UNDER_PRECOMMIT_TOTAL = {
        "precommit.tracker_and_roadmap",
        "precommit.coverage_gate_fire", "precommit.coverage_gate_join",
        "precommit.review_trail",
    }
    top_level_total = sum(
        v for k, v in medians.items() if k not in _NESTED_UNDER_PRECOMMIT_TOTAL
    )
    print(f"\nSum of NESTING-CORRECTED top-level step medians: {top_level_total:.2f} ms")
    print(f"(naive flat sum, double-counts precommit_tail_total's nesting): {sum(medians.values()):.2f} ms")
    print(f"Median wall-clock (_handler call, start-to-return): {total_elapsed_median * 1000:.2f} ms")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Spike driver: process time and spawn count of deliverable.cascade_terminal on a corpus the size of the live one.

Builds a throwaway git repo under the OS temp dir holding CORPUS_HANDOFFS handoffs and CORPUS_SIZINGS
sizings (the census counts of the live corpus), then measures four cases, each CASE_RUNS times, as
median and max process time (time.process_time, never wall clock) plus subprocess count:

  a  zero candidates for the deliverable_id (the common case), in-process, warm
  b  one live peer that clears the predicate: one advance, one commit_paths commit, in-process, warm
  c  case (a) in a fresh interpreter, cold, the way close.background runs after spawn_detached
  d  halt, baton not terminal: the run-baton.json read plus one frontmatter read, no op call

It also records close.background's cumulative process time for its four existing legs on
close_background_full_path's fixture, each run in a fresh interpreter, so the 400ms job budget
headroom is a number. The verdict line is within-bar when (a) and (b) are under 200ms, (a) spawns 0,
and the existing legs plus the larger of (a)/(b) fit under 400ms.

    python coordinator_core/benchmarks/run_baton_cascade_full_path.py

Exits 0 once every case ran; the verdict is the printed `verdict:` line. `--child` and `--job-child`
are the internal fresh-interpreter entries.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.git.run import run_git  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

CORPUS_HANDOFFS = 757
CORPUS_SIZINGS = 1253
CASE_RUNS = 5
OP_BAR_MS = 200.0
JOB_BUDGET_MS = 400.0
SESSION_ID = "11111111-2222-3333-4444-555555555555"
DID_NONE = "dlv-bench-nothing"
DID_ONE = "dlv-bench-onepeer"
SOURCE = "state/handoffs/2026-10-21-source.md"
PEER = "state/handoffs/2026-10-21-peer.md"
RUN_ID = "bench-run"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str) -> str:
    done = run_git(["-C", str(repo), *args], env=_GIT_ENV)
    if done.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed rc={done.returncode} timed_out={done.timed_out}: {done.stderr.strip()}"
        )
    return done.stdout.strip()


def _handoff(name: str, deliverable_id: str, deployment_state: str = "ready_to_fire") -> str:
    return (
        f'---\ntitle: "Bench Handoff {name}"\ncreated: 2026-01-01\nbranch: work/test/2026-01-01\n'
        f'status: open\npredecessor: "none"\ndeployment_state: {deployment_state}\n'
        f"deliverable_id: {deliverable_id}\n---\n\n# Handoff\n\nBody.\n"
    )


def _sizing(deliverable_id: str) -> str:
    return "\n".join([
        "schema: sizing-object", "intent: Bench intent, verbatim.", "estimate:", "  tshirt: M",
        "  provisional: true", "route: plan", "detents: []", "fork: null", "xl_exit: null",
        "status: routed", "premise:", "  provenance: read", "  evidence: bench fixture",
        f"deliverable_id: {deliverable_id}", "",
    ])


def build_corpus(parent: Path, peer: bool) -> tuple[Path, str]:
    """A committed repo with the census-sized corpus plus the source baton; `peer` adds one live
    handoff joined to DID_ONE. Returns (repo, base_sha)."""
    repo = parent / ("with-peer" if peer else "no-peer")
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    handoffs = repo / "state" / "handoffs"
    sizings = repo / "state" / "sizings"
    handoffs.mkdir(parents=True)
    sizings.mkdir(parents=True)
    for i in range(CORPUS_HANDOFFS - 1 - int(peer)):
        (handoffs / f"20260101-pad-{i:04d}.md").write_text(
            _handoff(f"pad-{i:04d}", f"dlv-pad-{i:04d}"), encoding="utf-8")
    for i in range(CORPUS_SIZINGS):
        (sizings / f"20260101-pad-{i:04d}.yaml").write_text(_sizing(f"dlv-pad-{i:04d}"), encoding="utf-8")
    did = DID_ONE if peer else DID_NONE
    (repo / SOURCE).write_text(_handoff("source", did, "shipped"), encoding="utf-8")
    if peer:
        (repo / PEER).write_text(_handoff("peer", did), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"seed\n\nSession-Id: {SESSION_ID}")
    return repo, _git(repo, "rev-parse", "HEAD")


def _params(did: str, base: str) -> dict:
    return {"deliverable_id": did, "source_kind": "handoff", "source_path": SOURCE, "ship_sha": base}


class _Spawns:
    """Counts subprocess.Popen constructions while active."""

    def __enter__(self) -> "_Spawns":
        self.argvs: list[tuple[str, ...]] = []
        self._real = subprocess.Popen
        argvs, real = self.argvs, self._real

        class _Popen(real):  # type: ignore[misc,valid-type]
            def __init__(self, args, *a, **kw):
                argvs.append(tuple(str(x) for x in args) if isinstance(args, (list, tuple)) else (str(args),))
                super().__init__(args, *a, **kw)

        subprocess.Popen = _Popen  # type: ignore[misc]
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen = self._real  # type: ignore[misc]


def _dispatch(did: str, base: str, repo: Path) -> dict:
    import coordinator_core.ops.cascade_terminal_op  # noqa: F401  (registers the op)
    from coordinator_core.warm.entry_seam import reentrant_dispatch

    return reentrant_dispatch("deliverable.cascade_terminal", _params(did, base), repo_root=repo / ".git")


def _measure(did: str, base: str, repo: Path) -> tuple[float, int, dict]:
    with _Spawns() as spawns:
        start = time.process_time()
        result = _dispatch(did, base, repo)
        elapsed = (time.process_time() - start) * 1000.0
    return elapsed, len(spawns.argvs), result


def _stats(samples: list[float]) -> tuple[float, float]:
    return statistics.median(samples), max(samples)


def case_warm(repo: Path, base: str, did: str, expect_advance: bool) -> dict:
    _dispatch(did, base, repo)  # warm: imports and first read
    _git(repo, "reset", "-q", "--hard", base)
    times: list[float] = []
    spawn_counts: list[int] = []
    for _ in range(CASE_RUNS):
        _git(repo, "reset", "-q", "--hard", base)
        elapsed, spawned, result = _measure(did, base, repo)
        advanced = len(result["by_kind"]["handoff"]["advanced"])
        if expect_advance and (advanced != 1 or not result.get("commit_sha") or "commit_error" in result):
            raise SystemExit(f"case did not advance its peer: {result!r}")
        if not expect_advance and (advanced or result.get("commit_sha") or result["by_kind"]["handoff"]["candidates_matched"]):
            raise SystemExit(f"case found a candidate it should not have: {result!r}")
        times.append(elapsed)
        spawn_counts.append(spawned)
    median, worst = _stats(times)
    return {"median_ms": median, "max_ms": worst, "spawns": max(spawn_counts), "samples_ms": times}


def _run_child(args: list[str]) -> list[str]:
    done = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *args], capture_output=True, text=True,
        env=_GIT_ENV, stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )
    if done.returncode != 0:
        raise SystemExit(f"child {args[0]} failed: {done.stderr.strip()[-400:]}")
    return done.stdout.strip().splitlines()


def case_cold(repo: Path, base: str) -> dict:
    totals: list[float] = []
    dispatches: list[float] = []
    spawn_counts: list[int] = []
    for _ in range(CASE_RUNS):
        out = json.loads(_run_child(["--child", str(repo), DID_NONE, base])[-1])
        totals.append(out["process_total_ms"])
        dispatches.append(out["dispatch_ms"])
        spawn_counts.append(out["spawns"])
    median, worst = _stats(dispatches)
    t_median, t_worst = _stats(totals)
    return {"median_ms": median, "max_ms": worst, "total_median_ms": t_median, "total_max_ms": t_worst,
            "spawns": max(spawn_counts), "samples_ms": dispatches}


def case_halt_not_terminal(parent: Path) -> dict:
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
    from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
    from coordinator_core.ops.dispatch_emit import ask_contract

    repo = parent / "halt"
    (repo / "state" / "handoffs").mkdir(parents=True)
    baton_rel = "state/handoffs/2026-10-21-open.md"
    (repo / baton_rel).write_text(_handoff("open", DID_ONE), encoding="utf-8")
    record = ask_contract.record_dir(repo, RUN_ID)
    record.mkdir(parents=True)
    (record / ask_contract.RUN_BATON_FILE).write_text(json.dumps({"baton": baton_rel}), encoding="utf-8")

    def halt_check() -> bool:
        rel = ask_contract.read_run_baton(repo, RUN_ID)
        text = (repo / rel).read_text(encoding="utf-8")
        split = split_frontmatter(text)
        return read_fm_field_unquoted(split.fm_text, "deployment_state") in HANDOFF_TERMINAL_DEPLOYMENT

    halt_check()
    times: list[float] = []
    spawn_counts: list[int] = []
    for _ in range(CASE_RUNS):
        with _Spawns() as spawns:
            start = time.process_time()
            terminal = halt_check()
            times.append((time.process_time() - start) * 1000.0)
        if terminal:
            raise SystemExit("halt case baton read as terminal")
        spawn_counts.append(len(spawns.argvs))
    median, worst = _stats(times)
    return {"median_ms": median, "max_ms": worst, "spawns": max(spawn_counts), "samples_ms": times}


def case_job_legs() -> dict:
    totals: list[float] = []
    for _ in range(CASE_RUNS):
        with tempfile.TemporaryDirectory(prefix="bench-job-") as tmp:
            out = json.loads(_run_child(["--job-child", tmp])[-1])
        totals.append(out["job_ms"])
    median, worst = _stats(totals)
    return {"median_ms": median, "max_ms": worst, "samples_ms": totals}


def _child(repo: Path, did: str, base: str) -> int:
    declare_benchmark_origin()
    os.environ["COORDINATOR_SESSION_ID"] = SESSION_ID
    import coordinator_core.ops.cascade_terminal_op  # noqa: F401
    from coordinator_core.warm.entry_seam import reentrant_dispatch

    with _Spawns() as spawns:
        start = time.process_time()
        reentrant_dispatch("deliverable.cascade_terminal", _params(did, base), repo_root=repo / ".git")
        dispatch_ms = (time.process_time() - start) * 1000.0
    print(json.dumps({"dispatch_ms": dispatch_ms, "process_total_ms": time.process_time() * 1000.0,
                      "spawns": len(spawns.argvs)}))
    return 0


def _job_child(parent: Path) -> int:
    declare_benchmark_origin()
    os.environ["COORDINATOR_SESSION_ID"] = SESSION_ID
    from coordinator_core.benchmarks import close_background_full_path as cbf

    repo = cbf.build_fixture(parent)
    _git(repo, "checkout", "-q", "-b", "work/bench/2026-10-21")
    from coordinator_core.ops.ceremony import close_background  # noqa: F401

    start = time.process_time()
    outcomes = cbf._run_job(repo)
    job_ms = (time.process_time() - start) * 1000.0
    bad = {leg: o for leg, o in outcomes.items() if getattr(o, "status", None) != "ran"}
    if bad:
        raise SystemExit(f"job legs did not all run: {bad!r}")
    print(json.dumps({"job_ms": job_ms}))
    return 0


def _row(name: str, label: str, r: dict) -> str:
    return f"  {name}  {label:<52} median {r['median_ms']:7.1f}ms  max {r['max_ms']:7.1f}ms  spawns {r['spawns']}"


def main(argv: list[str]) -> int:
    if argv[:1] == ["--child"] and len(argv) == 4:
        return _child(Path(argv[1]), argv[2], argv[3])
    if argv[:1] == ["--job-child"] and len(argv) == 2:
        return _job_child(Path(argv[1]))
    if argv:
        print("usage: run_baton_cascade_full_path.py", file=sys.stderr)
        return 2
    declare_benchmark_origin()
    os.environ["COORDINATOR_SESSION_ID"] = SESSION_ID
    with tempfile.TemporaryDirectory(prefix="bench-cascade-") as tmp:
        parent = Path(tmp)
        repo_a, base_a = build_corpus(parent, peer=False)
        repo_b, base_b = build_corpus(parent, peer=True)
        n_h = len(list((repo_a / "state" / "handoffs").glob("*.md")))
        n_s = len(list((repo_a / "state" / "sizings").glob("*.yaml")))
        a = case_warm(repo_a, base_a, DID_NONE, expect_advance=False)
        b = case_warm(repo_b, base_b, DID_ONE, expect_advance=True)
        c = case_cold(repo_a, base_a)
        d = case_halt_not_terminal(parent)
    legs = case_job_legs()
    print(f"fixture: {n_h} handoffs, {n_s} sizings; {CASE_RUNS} runs per case; process time, not wall clock")
    print(_row("a", "zero candidates, warm in-process", a))
    print(_row("b", "one peer advanced + one commit, warm in-process", b))
    print(_row("c", "zero candidates, fresh interpreter (dispatch only)", c))
    print(f"     c  fresh interpreter whole-process total            median {c['total_median_ms']:7.1f}ms  max {c['total_max_ms']:7.1f}ms")
    print(_row("d", "halt, baton not terminal (two reads)", d))
    print(f"  job  close.background existing four legs, fresh interpreter  median {legs['median_ms']:7.1f}ms  max {legs['max_ms']:7.1f}ms  (budget {JOB_BUDGET_MS:.0f}ms)")
    worst_op = max(a["max_ms"], b["max_ms"])
    headroom = JOB_BUDGET_MS - legs["max_ms"]
    checks = {
        "a and b under 200ms": a["max_ms"] < OP_BAR_MS and b["max_ms"] < OP_BAR_MS,
        "a spawns 0": a["spawns"] == 0,
        "legs + larger of a/b under 400ms": legs["max_ms"] + worst_op < JOB_BUDGET_MS,
    }
    print(f"  job headroom {headroom:.1f}ms; legs max + larger of a/b = {legs['max_ms'] + worst_op:.1f}ms")
    for name, ok in checks.items():
        print(f"  check {name}: {'pass' if ok else 'FAIL'}")
    print(f"verdict: {'within-bar' if all(checks.values()) else 'over-bar'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

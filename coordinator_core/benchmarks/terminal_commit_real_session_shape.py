"""Driver for `dispatch.terminal_commit` on a real session's shape, measured as a process tree.

The session is the one the 2026-10-27 08:20Z recurrence ran in: a Claude session dir holding
WF_DIRS `wf_` dirs and WF_DIRS * AGENTS_PER_WF settled agent metas, the run's own `wf_` dir with its
unsettled `commit` agent, and a wave-bookkeeping stem in the call's `inline_review`. The landing is
the ask-run commit through `ceremony.commit_v2` in a git repo under the caller's directory.
Evidence for the red this gate was written against is the C2 real-shape measurement
(docs/research/spike-verdicts/2026-10-27-warm-mutating-op-process-cost.md), not a checkout of
2d96cf63f5.

    python -m coordinator_core.benchmarks.terminal_commit_real_session_shape <dir> [--setup-only | --phases]

`--setup-only` does everything except the `terminal_commit._handler` call, so the cost of the call
is `full - setup-only`. `--phases` runs the same call under an in-process per-leg timer and prints one
row per leg (exclusive process ms, spawns, calls) plus how many landed paths stat-match their index
entry; its rows partition the call's total, so they sum to it by construction. cProfile is a separate drill-down, never part of the table.
Everything lives under `<dir>`: the session tree is reached through `CLAUDE_CONFIG_DIR`, so no path of
the host's `~/.claude` is read.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Iterator

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks import terminal_commit_full_path as base
from coordinator_core.git.run import run_git

WF_DIRS = 320
AGENTS_PER_WF = 14
AGENT_METAS = WF_DIRS * AGENTS_PER_WF
WAVE_STEM = f"{base.PLAN_ID}.review-wave-bookkeeping"
WAVE_SIDECARS = 3
LANDING_PATHS = 7_500
SLUG = "real-session-shape"
RESULT_FILE = "handler.json"


def _jsonl(rows: list) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def build_session_tree(config_dir: Path, session_id: str, run_id: str, wf_dirs: int = WF_DIRS) -> Path:
    """`<config_dir>/projects/<slug>/<session_id>` with `wf_dirs` settled `wf_` dirs plus the run's own
    live one; returns the session dir. The live dir's commit agent is the only unsettled agent."""
    workflows = config_dir / "projects" / SLUG / session_id / "subagents" / "workflows"
    workflows.mkdir(parents=True)
    for i in range(wf_dirs):
        wf = workflows / f"wf_{i:05d}"
        wf.mkdir()
        events = []
        for a in range(AGENTS_PER_WF):
            agent = f"a{i:05d}x{a:02d}"
            events.append({"type": "started", "agentId": agent, "label": "work"})
            events.append({"type": "result", "agentId": agent})
            (wf / f"agent-{agent}.meta.json").write_text(json.dumps({"description": "work"}), encoding="utf-8")
            (wf / f"agent-{agent}.jsonl").write_text("{}\n", encoding="utf-8")
        (wf / "journal.jsonl").write_text(_jsonl(events), encoding="utf-8")
    live = workflows / "wf_live"
    live.mkdir()
    prompt = {"message": {"content": f"commit via scratch/warp/{run_id}/terminal-commit-params.json"}}
    (live / "agent-c1.meta.json").write_text(json.dumps({"description": "commit"}), encoding="utf-8")
    (live / "agent-c1.jsonl").write_text(_jsonl([prompt]), encoding="utf-8")
    (live / "journal.jsonl").write_text(
        _jsonl([{"type": "started", "agentId": "c1", "label": "commit"}]), encoding="utf-8"
    )
    return workflows.parent.parent


def _wave_inline_review(repo: Path) -> dict:
    sidecars = []
    for i in range(WAVE_SIDECARS):
        rel = f".coordinator-local/subagent-share/fixture-run/wave-{i}.md"
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nverdict: PASS\nplan_id: {base.PLAN_ID}\n---\n\nwave review {i}\n", encoding="utf-8")
        sidecars.append(rel)
    review = json.loads(json.dumps(base.INLINE_REVIEW))
    review["integration_stem"] = WAVE_STEM
    review["wave_sidecar_paths"] = sidecars
    review["prep_sidecar"] = base.REVIEW_SIDECAR_REL
    head = run_git(["rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()
    review["prep"]["run_base_sha"] = head
    return review


def _widen_landing(repo: Path, landing_paths: int) -> None:
    """Grow the ask run's single chunk to `landing_paths` product files (LANDING_PATHS is the path-set
    size of the incident's `ceremony.commit_v2` call) by rewriting the run's commit-request marker."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    pkg = repo / "pkg"
    pkg.mkdir()
    rels = []
    for i in range(landing_paths):
        (pkg / f"f{i:05d}.py").write_text(f"v = {i}\n", encoding="utf-8")
        rels.append(f"pkg/f{i:05d}.py")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py", *rels)),),
        deliverable_id=base.ASK_DLV,
        plan_path=base.ASK_PLAN_REL,
    )
    marker = repo / RUN_DIR_ROOT / base.ASK_RUN / "commit-request.txt"
    marker.write_text(render_marker(request) + "\n", encoding="utf-8")


def build_fixture(
    parent: Path, *, wf_dirs: int = WF_DIRS, landing_paths: int = LANDING_PATHS
) -> tuple[Path, dict]:
    """A fresh ask-run repo plus the session tree under `parent`; returns the repo root and the
    `dispatch.terminal_commit` params. Points `CLAUDE_CONFIG_DIR` at the tree and reads the repo as
    onboarded, so the call writes its run artifacts."""
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    repo, script = base.build_ask_fixture(parent)
    process_artifact_policy.hosts_coordinator_state = lambda root: True
    config_dir = repo.parent / f"{repo.name}-claude"
    build_session_tree(config_dir, base.SESSION_ID, base.ASK_RUN, wf_dirs)
    os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
    os.environ.pop("CLAUDE_HOME", None)
    os.environ["COORDINATOR_SESSION_ID"] = base.SESSION_ID
    from coordinator_core.session import core as session_core

    session_core.init(base.SESSION_ID, cwd=str(repo))
    _widen_landing(repo, landing_paths)
    params = {
        "script_path": script,
        "incomplete_chunks": [],
        "session_id": base.SESSION_ID,
        "inline_review": _wave_inline_review(repo),
        "ledger_verdict": {"items": [{"id": "R1", "status": "met"}]},
    }
    return repo, params


def stub_close_job(repo: Path) -> None:
    """Point the close-job launch at a no-op script, so the landing queues its close legs and no
    real `close.background` runs in the measured tree (that job is gated by
    `test_close_background_process_budget.py`). Trap: `_launch_close_job` renamed makes this a no-op."""
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached
    from coordinator_core.ops.dispatch_emit import terminal_commit

    if not hasattr(terminal_commit, "_launch_close_job"):
        raise RuntimeError("terminal_commit._launch_close_job renamed: this driver would run the real close job")
    noop = repo.parent / "close_job_noop.py"
    noop.write_text("pass\n", encoding="utf-8")
    engine = Path(__file__).resolve().parents[2]

    def launch(root: Path, run_id: str, batons: list, memo: object) -> bool:
        return spawn_detached(str(engine), str(noop), [run_id, "--root", str(root)], site_free=True)

    terminal_commit._launch_close_job = launch


def land(repo: Path, params: dict) -> dict:
    from coordinator_core.ops.dispatch_emit import terminal_commit

    return terminal_commit._handler(params, repo_root=repo / ".git")


OUTSIDE = "(outside any leg)"


def _child_cpu_secs() -> float:
    """CPU seconds of reaped children: the engine's per-op bucket (what `run_git` charges on Windows)
    plus `RUSAGE_CHILDREN` where POSIX populates it."""
    ipc = sys.modules.get("coordinator_core.ipc")
    bucket = getattr(getattr(ipc, "_op_cpu_secs_var", None), "get", lambda: None)()
    secs = float(sum(bucket)) if bucket else 0.0
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        secs += usage.ru_utime + usage.ru_stime
    except ImportError:
        pass
    return secs


class PhaseTimer:
    """Exclusive per-leg process time and spawn counts for one in-process call.

    Time and spawns are charged to the innermost open leg, so the rows partition the call and sum to
    `total_ms`; a child's CPU lands on the leg that was open when it was reaped. `inclusive` adds the
    nested legs back. Spawns come from the `subprocess.Popen` audit event.
    """

    def __init__(self) -> None:
        self._stack: list[str] = []
        self.exclusive_ms: dict[str, float] = {}
        self.inclusive_ms: dict[str, float] = {}
        self.spawns: dict[str, int] = {}
        self.calls: dict[str, int] = {}
        self._spawn_count = 0
        self._mark = (0.0, 0)

    def _now(self) -> tuple[float, int]:
        return (time.process_time() + _child_cpu_secs()) * 1000.0, self._spawn_count

    def _charge(self) -> tuple[float, int]:
        now = self._now()
        name = self._stack[-1] if self._stack else OUTSIDE
        self.exclusive_ms[name] = self.exclusive_ms.get(name, 0.0) + (now[0] - self._mark[0])
        self.spawns[name] = self.spawns.get(name, 0) + (now[1] - self._mark[1])
        self._mark = now
        return now

    def _on_audit(self, event: str, args: tuple) -> None:
        if event == "subprocess.Popen":
            self._spawn_count += 1

    @contextlib.contextmanager
    def measuring(self) -> Iterator[None]:
        """Open the whole call; `total_ms` and `total_spawns` are set on exit."""
        sys.addaudithook(self._on_audit)
        self._mark = start = self._now()
        try:
            yield
        finally:
            end = self._charge()
            self.total_ms = end[0] - start[0]
            self.total_spawns = end[1] - start[1]
            self._spawn_count = -(1 << 60)  # the audit hook cannot be removed; park it

    @contextlib.contextmanager
    def leg(self, name: str) -> Iterator[None]:
        entered = self._charge()
        self._stack.append(name)
        self.calls[name] = self.calls.get(name, 0) + 1
        try:
            yield
        finally:
            left = self._charge()
            self._stack.pop()
            self.inclusive_ms[name] = self.inclusive_ms.get(name, 0.0) + (left[0] - entered[0])

    def rows(self) -> list[dict]:
        names = sorted(self.exclusive_ms, key=lambda n: -self.exclusive_ms[n])
        return [
            {
                "leg": n,
                "process_ms": round(self.exclusive_ms[n], 3),
                "inclusive_ms": round(self.inclusive_ms.get(n, self.exclusive_ms[n]), 3),
                "spawns": self.spawns.get(n, 0),
                "calls": self.calls.get(n, 0),
            }
            for n in names
        ]


def install_phase_probes(timer: PhaseTimer) -> Callable[[], None]:
    """Wrap the pre-commit legs and every `commit_v2` ledger phase in `timer`; returns the restore."""
    from coordinator_core.git import commit as git_commit
    from coordinator_core.ops.ceremony import commit_v2, commit_v2_phases
    from coordinator_core.session import scope as session_scope
    from coordinator_core.usage import run_locate

    restores: list[tuple[object, str, object]] = []

    def wrap(owner: object, attr: str, label: str) -> None:
        original = getattr(owner, attr)

        @functools.wraps(original)
        def timed(*a, **k):
            with timer.leg(label):
                return original(*a, **k)

        setattr(owner, attr, timed)
        restores.append((owner, attr, original))

    wrap(run_locate, "locate_run_dir", "locate_run_dir")
    wrap(session_scope, "contested_by_live_peers", "contested_by_live_peers")
    wrap(git_commit, "_checkin_bytes", "_checkin_bytes")
    wrap(commit_v2, "_pre_commit_gates", "_pre_commit_gates")

    phase = commit_v2_phases.PhaseLedger.phase

    def timed_phase(self, name):
        @contextlib.contextmanager
        def both():
            with timer.leg(f"phase:{name}"), phase(self, name):
                yield

        return both()

    commit_v2_phases.PhaseLedger.phase = timed_phase
    restores.append((commit_v2_phases.PhaseLedger, "phase", phase))

    def restore() -> None:
        for owner, attr, original in reversed(restores):
            setattr(owner, attr, original)

    return restore


def index_stat_match(repo: Path, rels: list[str]) -> dict:
    """How many of `rels` have an index entry, and how many of those match the file's size and mtime
    seconds (the premise of skipping a checkin hash). One `git ls-files --debug` spawn."""
    out = run_git(["ls-files", "--debug", "--", *rels], cwd=str(repo)).stdout.splitlines()
    entries: dict[str, dict] = {}
    current = None
    for line in out:
        if not line.startswith(" "):
            current = entries.setdefault(line.strip(), {})
            continue
        key, _, value = line.strip().partition(":")
        if current is not None and key in ("mtime", "size"):
            current[key] = value.split()[0]
    matched = 0
    for rel, entry in entries.items():
        try:
            st = (repo / rel).stat()
        except OSError:
            continue
        if entry.get("size") == str(st.st_size) and entry.get("mtime", "").split(":")[0] == str(int(st.st_mtime)):
            matched += 1
    return {"landed": len(rels), "in_index": len(entries), "stat_match": matched}


def format_table(timer: PhaseTimer, stat: dict) -> str:
    lines = [f"{'leg':44} {'process_ms':>11} {'incl_ms':>10} {'spawns':>7} {'calls':>6}"]
    for r in timer.rows():
        lines.append(f"{r['leg']:44} {r['process_ms']:11.1f} {r['inclusive_ms']:10.1f} {r['spawns']:7d} {r['calls']:6d}")
    lines.append(f"{'TOTAL (sum of rows)':44} {sum(timer.exclusive_ms.values()):11.1f} {'':10} {sum(timer.spawns.values()):7d}")
    lines.append(f"landed paths {stat['landed']}: {stat['in_index']} in index, {stat['stat_match']} stat-match")
    return "\n".join(lines)


def run_phases(repo: Path, params: dict) -> tuple[dict, PhaseTimer, dict]:
    """The default `terminal_commit._handler` call under `PhaseTimer`; returns (reply, timer, stat)."""
    from coordinator_core import ipc

    request = repo / "pkg"
    rels = sorted(f"pkg/{p.name}" for p in request.glob("*.py")) if request.is_dir() else []
    stat = index_stat_match(repo, rels)
    timer = PhaseTimer()
    restore = install_phase_probes(timer)
    token = ipc._op_cpu_secs_var.set([])
    try:
        with timer.measuring():
            out = land(repo, params)
    finally:
        ipc._op_cpu_secs_var.reset(token)
        restore()
    return out, timer, stat


def main(argv: list[str]) -> int:
    declare_benchmark_origin()
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a not in ("--setup-only", "--phases")]
    if len(args) != 1 or unknown or ("--setup-only" in argv and "--phases" in argv):
        print("usage: terminal_commit_real_session_shape <dir> [--setup-only | --phases]", file=sys.stderr)
        return 2
    repo, params = build_fixture(Path(args[0]))
    from coordinator_core.ops.dispatch_emit import terminal_commit  # noqa: F401  (loaded in both modes)

    stub_close_job(repo)
    if "--setup-only" in argv:
        return 0
    if "--phases" in argv:
        out, timer, stat = run_phases(repo, params)
        print(format_table(timer, stat))
        (repo.parent / RESULT_FILE).write_text(
            json.dumps({"handler_ms": timer.total_ms, "rows": timer.rows(), "stat": stat}), encoding="utf-8"
        )
        if out.get("committed") is not True or not out.get("sha"):
            print(f"terminal_commit did not land: {out!r}", file=sys.stderr)
            return 1
        return 0
    started = time.process_time()
    out = land(repo, params)
    handler_ms = (time.process_time() - started) * 1000.0
    (repo.parent / RESULT_FILE).write_text(json.dumps({"handler_ms": handler_ms}), encoding="utf-8")
    if out.get("committed") is not True or not out.get("sha"):
        print(f"terminal_commit did not land: {out!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

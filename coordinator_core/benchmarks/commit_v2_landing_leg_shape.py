"""Driver for the `dispatch.terminal_commit` landing leg at a heavy-claim session shape.

Builds a repo and a session whose touch record holds CLAIMED_IGNORED written, gitignored
`.coordinator-local/` files plus LANDABLE_LEFT genuinely landable left-behind files, then lands one
mixed product+process terminal commit through `terminal_commit._handler`. The leg is every
`ceremony.commit_v2` call that landing makes: the product commit, the own-left-behind follow-on and
the coded stamp. For each call it records the paths handed, the process time and the spawn count.

The gated figure sums one bracket per leg step (each commit_v2 dispatch and the follow-on's ignore
answer): this process's `time.process_time()` plus descendants via a fresh
`commit_leg_profile._ChildMeter` each (job object on Windows, RUSAGE_CHILDREN on POSIX). The
whole-handler figure is reported as `handler_process_time_ms`, a diagnostic. Per-call figures are
tick-quantised and never asserted on. The fixture repo runs no
commit hook, so there is no hook cost to report apart from the leg. Wall clock is never recorded.

    python -m coordinator_core.benchmarks.commit_v2_landing_leg_shape <dir>

prints the leg report as JSON. The fixture shape follows commit_v2_terminal_shape_full_path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter
from coordinator_core.git.run import run_git

SELF = "11e5f000-0000-4000-8000-0000000000c1"
CLAIMED_IGNORED = 7000
LANDABLE_LEFT = 2
# Extra tracked files in the seed commit, in a nested tree: the S1 clone's tracked-file count.
TRACKED_FILES = 59368
# Windows process_time resolution: the error of one bracket's own-CPU reading.
TICK_MS = 15.625
SHARE_DIR = ".coordinator-local/subagent-share/claimed"
OTHER_IGNORED_DIR = ".coordinator-local/scratchpad-other"
LANDABLE_PATHS = ("state/debt-backlog/d1.yaml", "state/debt-backlog/d2.yaml")
PRODUCT_PATH = "a.py"
PROCESS_PATH = "state/audits/a.yaml"
PLAN_PATH = "docs/plan.md"
SCRIPT_PATH = "run.mjs"

_PLAN = """---
title: p
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""


@dataclass
class CallRecord:
    """One `ceremony.commit_v2` call of the leg; `process_time_ms` is this process's CPU across the
    call, a tick-quantised diagnostic that excludes children."""

    paths: list
    process_time_ms: float
    spawns: int


@dataclass
class LegReport:
    """Every commit_v2 call of one landing, with the landing's reply. `process_time_ms` is the gated
    figure: the sum over the leg's brackets (each commit_v2 dispatch plus the follow-on's ignore
    answer), each own CPU plus descendants. `handler_process_time_ms` is the whole
    `terminal_commit._handler` call, a diagnostic nothing asserts on. `quantisation_error_ms` is one
    CPU tick per bracket."""

    calls: list = field(default_factory=list)
    reply: dict = field(default_factory=dict)
    process_time_ms: float = 0.0
    handler_process_time_ms: float = 0.0
    brackets: int = 0
    ignore_spawns: int = 0

    @property
    def quantisation_error_ms(self) -> float:
        return round(self.brackets * TICK_MS, 3)

    @property
    def spawns(self) -> int:
        return sum(c.spawns for c in self.calls) + self.ignore_spawns

    @property
    def handed_paths(self) -> list:
        return sorted({p for c in self.calls for p in c.paths})


def _git(repo: Path, *args: str) -> str:
    result = run_git(list(args), cwd=str(repo))
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed rc={result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_bulk(repo: Path, *args: str) -> None:
    """Fixture setup for the TRACKED_FILES-sized seed: `run_git` bounds a call at its process-time
    budget, which a 59k-file `git add` exceeds; setup is outside every gated bracket."""
    result = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed rc={result.returncode}: {result.stderr.strip()}")


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def bind_session_env() -> None:
    """Point the process at the fixture session; pytest callers set the same keys through monkeypatch."""
    os.environ["COORDINATOR_SESSION_ID"] = SELF
    os.environ.pop("CLAUDE_SESSION_ID", None)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


def build_fixture(parent: Path) -> Path:
    """A fresh repo under `parent`: tracked seed, ignored `.coordinator-local/`, a session whose touch
    record holds the product and process paths, the landable leftovers and CLAIMED_IGNORED ignored
    written files; the request script is written. Returns the repo root."""
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.session import core as session_core
    from coordinator_core.session import scope as session_scope
    from coordinator_core.session import touch_record

    repo = parent / ("lls-" + uuid.uuid4().hex[:10])
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo, ".gitignore", ".coordinator-local/\n")
    _write(repo, "README.md", "seed\n")
    _write(repo, PRODUCT_PATH, "head\n")
    _write(repo, PLAN_PATH, _PLAN)
    for i in range(TRACKED_FILES):
        _write(repo, f"tracked/d{i % 50:02d}/s{i % 7}/f{i:06d}.txt", f"{i}\n")
    _git_bulk(repo, "add", ".")
    _git_bulk(repo, "commit", "-q", "-m", "seed")

    session_core.init(SELF, cwd=str(repo))
    _write(repo, PRODUCT_PATH, "worktree\n")
    _write(repo, PROCESS_PATH, "x: 1\n")
    for rel in LANDABLE_PATHS:
        _write(repo, rel, "x: 1\n")
    claimed = [PRODUCT_PATH, PROCESS_PATH, *LANDABLE_PATHS]
    for i in range(CLAIMED_IGNORED):
        base = SHARE_DIR if i % 2 == 0 else OTHER_IGNORED_DIR
        rel = f"{base}/f{i:05d}.md"
        _write(repo, rel)
        claimed.append(rel)
    for rel in claimed:
        session_scope.touch(
            SELF, rel, cwd=str(repo), root=str(repo), kind=touch_record.KIND_WRITE, refresh_activity=False
        )

    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=(PRODUCT_PATH, PROCESS_PATH)),),
        plan_path=PLAN_PATH,
    )
    _write(repo, SCRIPT_PATH, "// emitted\n" + render_marker(request) + "\n")
    return repo


def run_leg(repo: Path) -> LegReport:
    """Land the fixture's terminal commit and record each commit_v2 call; a call's spawns are the
    `subprocess.Popen` constructions inside it."""
    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

    report = LegReport()
    real_dispatch = terminal_commit.reentrant_dispatch
    real_init = subprocess.Popen.__init__
    spawned = [0]

    def counting_init(self, *a, **k):
        spawned[0] += 1
        return real_init(self, *a, **k)

    def bracketed(call):
        """Run `call()` inside a fresh own-CPU plus descendants bracket; returns the result;
        the bracket's (ms, spawns) land in `bracketed.last` and its ms is added to the leg figure."""
        before_spawns = spawned[0]
        child = _ChildMeter()
        child.start()
        cpu0 = time.process_time()
        try:
            return call()
        finally:
            ms = (time.process_time() - cpu0) * 1000.0 + child.stop_ms()
            report.process_time_ms = round(report.process_time_ms + ms, 3)
            report.brackets += 1
            bracketed.last = (round(ms, 3), spawned[0] - before_spawns)

    def spy(op, params, repo_root=None):
        if op != "ceremony.commit_v2":
            return real_dispatch(op, params, repo_root=repo_root)
        try:
            return bracketed(lambda: real_dispatch(op, params, repo_root=repo_root))
        finally:
            ms, spawns = bracketed.last
            report.calls.append(CallRecord(paths=list(params.get("paths") or []), process_time_ms=ms, spawns=spawns))

    real_ignored = terminal_commit._ignored_among_left

    def ignored_spy(*a, **k):
        try:
            return bracketed(lambda: real_ignored(*a, **k))
        finally:
            report.ignore_spawns += bracketed.last[1]

    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    real_hosts = process_artifact_policy.hosts_coordinator_state
    process_artifact_policy.hosts_coordinator_state = lambda root: True
    terminal_commit.reentrant_dispatch = spy
    terminal_commit._ignored_among_left = ignored_spy
    subprocess.Popen.__init__ = counting_init
    meter = _ChildMeter()
    meter.start()
    own0 = time.process_time()
    try:
        report.reply = terminal_commit._handler(
            {"script_path": SCRIPT_PATH, "incomplete_chunks": [], "inline_review": reviewed(),
             "session_id": SELF},
            repo_root=repo / ".git",
        )
    finally:
        own_ms = (time.process_time() - own0) * 1000.0
        report.handler_process_time_ms = round(own_ms + meter.stop_ms(), 3)
        process_artifact_policy.hosts_coordinator_state = real_hosts
        terminal_commit.reentrant_dispatch = real_dispatch
        terminal_commit._ignored_among_left = real_ignored
        subprocess.Popen.__init__ = real_init
    return report


def ignored_among(repo: Path, paths: list) -> list:
    """`paths` that git ignores in `repo`."""
    if not paths:
        return []
    result = run_git(
        ["check-ignore", "--stdin", "-z"], cwd=str(repo), input=("\0".join(paths) + "\0").encode("utf-8"),
    )
    return [p for p in result.stdout.split("\0") if p]


def main(argv: list[str]) -> int:
    declare_benchmark_origin()
    if len(argv) != 1:
        print("usage: commit_v2_landing_leg_shape <dir>", file=sys.stderr)
        return 2
    bind_session_env()
    repo = build_fixture(Path(argv[0]))
    report = run_leg(repo)
    print(json.dumps({
        "calls": [{**asdict(c), "paths": len(c.paths)} for c in report.calls],
        "process_time_ms": report.process_time_ms,
        "quantisation_error_ms": report.quantisation_error_ms,
        "handler_process_time_ms": report.handler_process_time_ms,
        "spawns": report.spawns,
        "committed": report.reply.get("committed"),
    }))
    return 0 if report.reply.get("committed") is True else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

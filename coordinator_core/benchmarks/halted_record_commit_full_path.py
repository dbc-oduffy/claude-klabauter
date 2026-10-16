"""Driver for the halted-run record-only commit, measured as a process tree.

Each invocation builds a fresh repo under a caller-given directory: an untracked
`state/warp/<run>/` holding the five record files a staged run writes, beside other dirty paths
(a modified tracked file a finished chunk declares, an untracked sizing, a second run's record
dir). It then calls `dispatch.terminal_commit`'s handler with `halted_by` params (no plan, no
incomplete_chunks); the run script's marker supplies the finished chunk.
`--setup-only` does everything except that call, so the cost of the call is `full - setup-only`.

    python -m coordinator_core.benchmarks.halted_record_commit_full_path <dir> [--setup-only]
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.git.run import run_git

RUN_ID = "ask-bench"
OTHER_RUN_ID = "ask-other"
RECORD_FILES = ("ask.md", "pm-intent.md", "run.json", "requirements.md", "manifest.json")


def _git(repo: Path, *args: str) -> str:
    done = run_git(list(args), cwd=str(repo))
    if not done.ok:
        raise subprocess.CalledProcessError(done.returncode, ["git", *args], done.stdout, done.stderr)
    return done.stdout


def build_fixture(parent: Path) -> Path:
    """A fresh repo with an untracked record dir plus other dirty paths; returns the repo root."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    repo = parent / uuid.uuid4().hex[:12]
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    (repo / "product.py").write_text("a = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    (repo / "product.py").write_text("a = 2\n", encoding="utf-8")
    (repo / "state" / "sizings").mkdir(parents=True)
    (repo / "state" / "sizings" / "x.yaml").write_text("schema: sizing-object\n", encoding="utf-8")
    record = repo / RECORD_DIR_ROOT / RUN_ID
    record.mkdir(parents=True)
    for name in RECORD_FILES:
        (record / name).write_text(f"{name}\n", encoding="utf-8")
    (repo / "reports").mkdir()
    (repo / "reports" / "C1.md").write_text("DONE: C1\n", encoding="utf-8")
    chunk = ChunkCommit(id="C1", title="C1", paths=("product.py",), report="reports/C1.md")
    marker = render_marker(CommitRequest(chunks=(chunk,), session_id=None, repo_root=None, plan_path=None))
    (repo / "run.mjs").write_text(f"{marker}\n", encoding="utf-8")
    other = repo / RECORD_DIR_ROOT / OTHER_RUN_ID
    other.mkdir(parents=True)
    (other / "ask.md").write_text("other\n", encoding="utf-8")
    return repo


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a != "--setup-only"]
    if len(args) != 1 or unknown:
        print("usage: halted_record_commit_full_path <dir> [--setup-only]", file=sys.stderr)
        return 2
    declare_benchmark_origin()
    repo = build_fixture(Path(args[0]))
    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    if "--setup-only" in argv:
        return 0
    out = terminal_commit._handler(
        {
            "script_path": "run.mjs", "run_id": RUN_ID,
            "halted_by": {"halted": "ledger-check", "reason": "sizing refused: route is not spec-dispatch"},
        },
        repo_root=repo / ".git",
    )
    expected = {f"{RECORD_DIR_ROOT}/{RUN_ID}/{n}" for n in (*RECORD_FILES, "halted.json")} | {"product.py"}
    if out.get("committed") is not True or set(out.get("paths") or []) != expected:
        print(f"halted-record commit did not complete: {out!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

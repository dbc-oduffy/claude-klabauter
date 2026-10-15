"""Driver for the grind terminal commit, measured as a process tree.

Each invocation builds a fresh repo under a caller-given directory: two finished rows with product
files, one row's ledger-root evidence path (dropped) and one untracked row deletion (dropped), beside
an undeclared dirty file. It then calls `dispatch.terminal_commit`'s handler with `grind` params.
`--setup-only` does everything except that call, so the cost of the call is `full - setup-only`.

    python -m coordinator_core.benchmarks.grind_terminal_commit_full_path <dir> [--setup-only]
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.git.run import run_git

RUN_ID = "20261015T000000Z"
PROFILE = "bug"
EVIDENCE = f"state/queue-grind/{PROFILE}/evidence/{RUN_ID}/r1.done.md"


def _git(repo: Path, *args: str) -> str:
    done = run_git(list(args), cwd=str(repo))
    if not done.ok:
        raise subprocess.CalledProcessError(done.returncode, ["git", *args], done.stdout, done.stderr)
    return done.stdout


def build_fixture(parent: Path) -> Path:
    """A fresh repo with two finished rows' product files and other dirt; returns the repo root."""
    repo = parent / uuid.uuid4().hex[:12]
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    (repo / "a.py").write_text("a = 2\n", encoding="utf-8")
    (repo / "b.py").write_text("b = 2\n", encoding="utf-8")
    (repo / "other.py").write_text("other = 1\n", encoding="utf-8")
    evidence = repo / EVIDENCE
    evidence.parent.mkdir(parents=True)
    evidence.write_text("done\n", encoding="utf-8")
    return repo


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a != "--setup-only"]
    if len(args) != 1 or unknown:
        print("usage: grind_terminal_commit_full_path <dir> [--setup-only]", file=sys.stderr)
        return 2
    declare_benchmark_origin()
    repo = build_fixture(Path(args[0]))
    from coordinator_core.ops.dispatch_emit import terminal_commit

    if "--setup-only" in argv:
        return 0
    out = terminal_commit._handler(
        {
            "script_path": "run.mjs", "run_id": RUN_ID,
            "grind": {
                "profile": PROFILE,
                "rows": [
                    {"row_id": "r1", "outcome": "fixed", "paths": ["a.py", EVIDENCE],
                     "deleted_paths": ["state/bug-backlog/r1.yaml"]},
                    {"row_id": "r2", "outcome": "fixed", "paths": ["b.py"], "deleted_paths": []},
                ],
            },
        },
        repo_root=repo / ".git",
    )
    if out.get("committed") is not True or len(out.get("rows") or []) != 2 or len(out.get("dropped") or []) != 2:
        print(f"grind terminal commit did not complete: {out!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

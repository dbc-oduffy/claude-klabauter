"""Driver for the dispatch.terminal_commit full path, measured as a process tree.

Each invocation builds a fresh repo under a caller-given directory (a one-row
plan with a met criterion, one DONE chunk), then drives the handler through the
whole close: product commit, review-stamp mint, coded-stamp commit, implemented
flip. `--setup-only` does everything except the handler call, so the cost of the
call is `full - setup-only` (the run-close spike's method).

    python -m coordinator_core.benchmarks.terminal_commit_full_path <dir> [--setup-only]

The argv is re-run k times by `batched_process_time_ms`, so every invocation
takes its own subdirectory; nothing is shared between runs.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

SESSION_ID = "11111111-2222-3333-4444-555555555555"
PLAN_ID = "pln-x-123456"
PLAN_REL = "docs/plan.md"

_PLAN = f"""---
title: p
status: executing
plan_id: {PLAN_ID}
prime_exit_criterion:
  text: a.py exists
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

INLINE_REVIEW = {
    "integration_stem": f"{PLAN_ID}.review-wave-bookkeeping",
    "slices": 1,
    "fixes": 0,
    "plan_id": PLAN_ID,
    "prep_sidecar": None,
    "wave_sidecar_paths": [],
    "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
    "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
    "tests": {"status": "not_run", "run": 0, "failed": 0, "sidecar": None},
    "criterion": {"status": "met", "observation": "a.py exists at HEAD", "sidecar": "j.md"},
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def build_fixture(parent: Path) -> tuple[Path, str]:
    """A fresh repo under `parent` with the plan committed and `a.py` written
    but unstaged; returns the repo root and the emitted script's repo-relative
    path."""
    from coordinator_core.ops.dispatch_emit.commit_request import (
        ChunkCommit,
        CommitRequest,
        render_marker,
    )

    repo = parent / uuid.uuid4().hex[:12]
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    (repo / "docs").mkdir()
    (repo / PLAN_REL).write_text(_PLAN, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "plan")
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path=PLAN_REL
    )
    script = "run.mjs"
    (repo / script).write_text("// emitted script\n" + render_marker(request) + "\n", encoding="utf-8")
    return repo, script


def _run_full_path(repo: Path, script: str) -> dict:
    from coordinator_core.ops.dispatch_emit import terminal_commit

    return terminal_commit._handler(
        {
            "script_path": script,
            "incomplete_chunks": [],
            "session_id": SESSION_ID,
            "inline_review": INLINE_REVIEW,
        },
        repo_root=repo / ".git",
    )


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a != "--setup-only"]
    if len(args) != 1 or unknown:
        print("usage: terminal_commit_full_path <dir> [--setup-only]", file=sys.stderr)
        return 2
    repo, script = build_fixture(Path(args[0]))
    from coordinator_core.ops.dispatch_emit import terminal_commit  # noqa: F401  (loaded in both modes)

    if "--setup-only" in argv:
        return 0
    out = _run_full_path(repo, script)
    if out.get("review_stamp") != "minted" or out.get("plan_status") != "implemented":
        print(f"full path did not complete: {out!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

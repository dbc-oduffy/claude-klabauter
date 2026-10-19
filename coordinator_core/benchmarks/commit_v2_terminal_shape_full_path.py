"""Driver for `ceremony.commit_v2` on the terminal-commit shape, measured as a process tree.

Each invocation builds a fresh repo under a caller-given directory: a session that owns RUNS
stopped runs, a legacy launch dir of LAUNCH_DIR_FILES unrelated files, and one landing of product
paths plus run-artifact paths. `--setup-only` does everything except the `commit_v2._handler`
call, so the cost of the call is `full - setup-only`.

    python -m coordinator_core.benchmarks.commit_v2_terminal_shape_full_path <dir> [--setup-only]

warp_live's managed and legacy launch dirs point into the caller's directory, so the host's real
tempdir is never listed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.win_portability import no_console_creationflags

SELF = "tsb-self"
SCRIPT = "scratch/warp/x.mjs"
RUNS = 12
LAUNCH_DIR_FILES = 2000


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stopped_run(repo: Path, transcripts: Path) -> str:
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit import run_token

    stamp = datetime.fromtimestamp(time.time() - 60, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_id = f"ask-{stamp}-{uuid.uuid4().hex[:6]}"
    launch = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "tu-" + run_id}]},
        "toolUseResult": {"status": "async_launched", "taskId": "w1"},
    }
    transcript = transcripts / f"t-{run_id}.jsonl"
    transcript.write_text(json.dumps(launch) + "\nSuccessfully stopped task: w1\n", encoding="utf-8")
    run_token.mint(repo, run_id, SCRIPT, session_id=SELF, sizing_rel=None)
    warp_live.record_submission(
        repo, run_id, session_id=SELF, transcript_path=str(transcript), script_path=SCRIPT,
        sizing_rel=None, tool_use_id="tu-" + run_id,
    )
    _write(repo, f"state/warp/{run_id}/requirements.md")
    return run_id


def terminal_params(repo: Path) -> dict:
    """Stage one terminal-shape landing (a product edit plus run artifacts); returns its params."""
    _write(repo, "product.py", f"a = {uuid.uuid4().int}\n")
    run = f"state/warp/ask-terminal-{uuid.uuid4().hex[:6]}"
    _write(repo, f"{run}/ask.md")
    _write(repo, f"{run}/run.json", "{}\n")
    return {
        "paths": ["product.py", f"{run}/ask.md", f"{run}/run.json"],
        "message": "terminal shape\n\nSession-Id: " + SELF + "\n",
    }


def build_fixture(parent: Path) -> tuple[Path, dict]:
    """A fresh repo under `parent` with RUNS stopped runs and one pending terminal-shape landing;
    returns the repo root and the commit_v2 params."""
    from coordinator_core import warp_live
    from coordinator_core.session import core as session_core

    base = parent / uuid.uuid4().hex[:12]
    managed = base / "managed"
    legacy = base / "legacy"
    transcripts = base / "transcripts"
    repo = base / "repo"
    for d in (managed, legacy, transcripts, repo):
        d.mkdir(parents=True)
    for i in range(LAUNCH_DIR_FILES):
        (legacy / f"unrelated-{i}.tmp").write_bytes(b"")
    warp_live.managed_tempdir = lambda: managed
    warp_live.legacy_top_level_path = lambda name: legacy / name

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo, "README.md", "seed\n")
    _write(repo, "product.py", "a = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    os.environ["COORDINATOR_SESSION_ID"] = SELF
    os.environ.pop("CLAUDE_SESSION_ID", None)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    session_core.init(SELF, cwd=str(repo))
    for _ in range(RUNS):
        _stopped_run(repo, transcripts)
    return repo, terminal_params(repo)


def main(argv: list[str]) -> int:
    declare_benchmark_origin()
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a != "--setup-only"]
    if len(args) != 1 or unknown:
        print("usage: commit_v2_terminal_shape_full_path <dir> [--setup-only]", file=sys.stderr)
        return 2
    repo, params = build_fixture(Path(args[0]))
    from coordinator_core.ops.ceremony import commit_v2  # noqa: F401  (loaded in both modes)

    if "--setup-only" in argv:
        return 0
    out = commit_v2._handler(params, repo_root=repo / ".git")
    if out.get("committed") is not True:
        print(f"terminal-shape commit did not land: {out!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

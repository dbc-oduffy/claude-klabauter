"""Driver for the no-script close, measured as a process tree.

Each invocation builds a fresh repo under a caller-given directory by landing a plan-run through
the scripted `terminal_commit` driver, then returns it to the landed-but-unclosed state: the
plan back to `executing` with its row `coded`, the completion entry and lessons outbox removed,
and the close-job record rewritten as one written before the no-script path existed (no
`criterion`, no `falsifier_broken`, no `review_receipt`, no `review` leg, `completion-entry`
`failed`). The task output supplies the falsifier mark. It then calls `close_landed_run`
through `dispatch.terminal_commit`'s handler (plan_path and run_id, no script_path) with the review launch stubbed: the dearest call the path admits (verdict from the
review record, the step-3 amend, the mint, the completion entry, the flip, the commit and the
step-7 amend). `--setup-only` does everything except that call, so the cost of the call is
`full - setup-only`.

    python -m coordinator_core.benchmarks.close_without_script_full_path <dir> [--setup-only]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks import terminal_commit_full_path as scripted
from coordinator_core.git.run import run_git

RUN_ID = "ask-bench.workflow"
SCRIPT = f"{RUN_ID}.mjs"
PLAN_REL = scripted.PLAN_REL
SESSION_ID = scripted.SESSION_ID


def _git(repo: Path, *args: str) -> str:
    done = run_git(list(args), cwd=str(repo))
    if not done.ok:
        raise subprocess.CalledProcessError(done.returncode, ["git", *args], done.stdout, done.stderr)
    return done.stdout


def write_task_output(
    repo: Path, falsifier_broken: list, criterion: dict | None = None, name: str = "task-output"
) -> str:
    """A digest naming this run's script, beside the repo; returns its absolute path."""
    import copy

    inline = copy.deepcopy(scripted.INLINE_REVIEW)
    if criterion is not None:
        inline["criterion"] = criterion
    path = repo.parent / f"{repo.name}-{name}.json"
    path.write_text(
        json.dumps(
            {"next_action": {"params": {
                "script_path": SCRIPT,
                "incomplete_chunks": [],
                "inline_review": inline,
                "falsifier_broken": falsifier_broken,
            }}}
        ),
        encoding="utf-8",
    )
    return str(path)


def build_fixture(parent: Path) -> tuple[Path, str]:
    """A landed, unclosed run; returns the repo root and a task output supplying a clear falsifier mark."""
    from coordinator_core.ops.dispatch_emit import run_close, terminal_commit

    repo, script = scripted.build_fixture(parent)
    (repo / script).rename(repo / SCRIPT)
    scripted._prepare_close(repo)
    landed = terminal_commit._handler(
        {
            "script_path": SCRIPT, "incomplete_chunks": [], "session_id": SESSION_ID,
            "inline_review": scripted.INLINE_REVIEW,
        },
        repo_root=repo / ".git",
    )
    if landed.get("review_stamp") != "minted":
        raise RuntimeError(f"fixture run did not land: {landed!r}")
    terminal_sha = landed["sha"]
    plan = repo / PLAN_REL
    plan.write_text(
        scripted._PLAN.replace(
            "  disposition: open\n", f"  disposition: coded\n  disposition_ref: {terminal_sha}\n"
        ),
        encoding="utf-8",
    )
    for entry in (repo / "archive" / "completed").rglob("*.md"):
        entry.unlink()
    for queued in (repo.parent / f"{repo.name}-lessons-outbox").glob("*.yaml"):
        queued.unlink()
    _git(repo, "add", "-A", PLAN_REL, "archive")
    _git(repo, "commit", "-q", "-m", "unclose fixture")
    record = run_close._load(repo, RUN_ID)
    for key in ("criterion", "falsifier_broken"):
        record.pop(key, None)
    record["review_receipt"] = None
    record["completion_entry"] = None
    record["legs"].pop("review", None)
    record["settled"]["completion-entry"] = {"status": "failed", "detail": "criterion absent"}
    run_close._write_atomic(repo / run_close.close_job_path(RUN_ID), record)
    return repo, write_task_output(repo, [])


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a != "--setup-only"]
    if len(args) != 1 or unknown:
        print("usage: close_without_script_full_path <dir> [--setup-only]", file=sys.stderr)
        return 2
    declare_benchmark_origin()
    repo, task_output = build_fixture(Path(args[0]))
    from coordinator_core.ops.ceremony import close_background
    from coordinator_core.ops.dispatch_emit import terminal_commit

    close_background._launch_close_review = lambda ctx: True
    if "--setup-only" in argv:
        return 0
    out = terminal_commit._handler(
        {"plan_path": PLAN_REL, "run_id": RUN_ID, "task_output_path": task_output, "session_id": SESSION_ID},
        repo_root=repo / ".git",
    )
    legs = (out.get("close") or {}).get("legs") or {}
    complete = (
        out.get("committed") is True and out.get("review_stamp") == "minted"
        and out.get("plan_status") == "implemented" and out.get("verdict_source") == "review-record"
        and (legs.get("completion-entry") or {}).get("status") == "ran"
        and (legs.get("review") or {}).get("status") == "queued"
    )
    if not complete:
        print(f"no-script close did not complete: {out!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

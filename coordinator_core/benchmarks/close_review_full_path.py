"""Driver for the close.review job, measured as a process tree.

Each invocation builds a fresh repo under a caller-given directory: HISTORY_COMMITS seeded
`Session-Id` commits (one `git fast-import` spawn) that touch 1-6 paths of a PATH_POOL-wide shared
pool, the last CHAIN_COMMITS of them also carrying the claimed baton's `Deliverable-Id`; one open
claimed handoff and a close-job record queueing the `review` leg. By default a tracked plan holds a
`review_stamp` at the commit GAP below the run base (the gap case); `--no-tip` leaves it out, so the
range walks to the cap. It then runs the job in-process. `--setup-only` does everything except the
job, so the cost of the job is `full - setup-only`.

    python -m coordinator_core.benchmarks.close_review_full_path <dir> [--setup-only] [--no-tip]
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.win_portability import no_console_creationflags

HISTORY_COMMITS = 3000
CHAIN_COMMITS = 700  # more than the review range cap, so the no-tip walk truncates
GAP = 50
PATH_POOL = 1200
DELIVERABLE = "dlv-bench-chain"
PLAN_REL = "docs/plans/prior.md"
RUN_ID = "bench-run"
SESSION_ID = "11111111-2222-3333-4444-555555555555"
BATON = "state/handoffs/h1.md"

_HANDOFF = (
    f'---\ntitle: h1\nstatus: open\ndeployment_state: unshipped\ndeliverable_id: "{DELIVERABLE}"\n---\n\nbody\n'
)


def _git(repo: Path, *args: str, stdin: str | None = None) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), input=None if stdin is None else stdin.encode(), capture_output=True,
        check=True, **no_console_creationflags(),
    )


def _history_stream(count: int, chain: int) -> str:
    """Commits touching 1-6 paths of a shared pool, so the walk's `--name-only` has real width."""
    parts = []
    for i in range(1, count + 1):
        trailers = f"Session-Id: {SESSION_ID}\n"
        if i > count - chain:
            trailers += f"Deliverable-Id: {DELIVERABLE}\n"
        message = f"c{i}\n\n{trailers}"
        parts.append(
            f"commit refs/heads/main\nmark :{i}\n"
            f"committer t <t@example.com> {1700000000 + i} +0000\n"
            f"data {len(message.encode())}\n{message}\n"
        )
        if i > 1:
            parts.append(f"from :{i - 1}\n")
        for k in range(1 + i % 6):
            body = f"{i}-{k}\n"
            slot = (i * 7 + k) % PATH_POOL
            parts.append(f"M 100644 inline src/d{slot % 60}/p{slot}.txt\ndata {len(body)}\n{body}\n")
    return "".join(parts)


def build_fixture(parent: Path, with_tip: bool = True) -> Path:
    from coordinator_core.ops.ceremony.close_review import REVIEW_RANGE_CAP
    from coordinator_core.ops.dispatch_emit import run_close

    assert CHAIN_COMMITS > REVIEW_RANGE_CAP, "chain must exceed the review range cap or no-tip never truncates"
    repo = parent.resolve() / uuid.uuid4().hex[:12]
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    marks = repo / ".git" / "bench-marks"
    _git(
        repo, "fast-import", "--quiet", f"--export-marks={marks}",
        stdin=_history_stream(HISTORY_COMMITS, CHAIN_COMMITS),
    )
    shas = dict(line.split()[:2] for line in marks.read_text(encoding="ascii").splitlines())
    run_base = shas[f":{HISTORY_COMMITS}"]
    tip = shas[f":{HISTORY_COMMITS - GAP}"]
    _git(repo, "checkout", "-q", "-f", "main")
    files = [(BATON, _HANDOFF)]
    if with_tip:
        stamp = f'---\ndeliverable_id: "{DELIVERABLE}"\nreview_stamp:\n  terminal_commit_sha: {tip}\n---\n'
        files.append((PLAN_REL, stamp))
    for rel, text in files:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    run_close.write_close_job(
        repo, RUN_ID, ("review",),
        datetime.now().astimezone() + timedelta(minutes=10),
        claimed_batons=(BATON,),
        review_receipt="docs/plans/current.md",
        run_base_sha=run_base,
        terminal_sha="f" * 40,
    )
    return repo


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a not in ("--setup-only", "--no-tip")]
    if len(args) != 1 or unknown:
        print("usage: close_review_full_path <dir> [--setup-only] [--no-tip]", file=sys.stderr)
        return 2
    declare_benchmark_origin()
    os.environ["COORDINATOR_SESSION_ID"] = SESSION_ID
    repo = build_fixture(Path(args[0]), with_tip="--no-tip" not in argv)
    from coordinator_core.ops.ceremony import close_review as cr

    if "--setup-only" in argv:
        return 0
    outcomes = cr.run_review(cr.JobContext(repo, repo / ".git", RUN_ID, (BATON,), None))
    # The fixture's chain commits are uncovered by design: the gap case names them, the no-tip case truncates.
    review = outcomes.get("review")
    expected = "range truncated" if "--no-tip" in argv else "unreceipted ancestor commits"
    if review is None or review.status != "failed" or expected not in review.detail:
        print(f"job did not complete: {outcomes!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

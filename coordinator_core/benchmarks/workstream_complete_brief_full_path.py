"""Driver for `workstream-complete-assemble brief`, measured as a process tree.

`--build` creates, once, a repo under a caller-given directory (later invocations reuse it): HISTORY_COMMITS seeded
commits carrying `Session-Id` trailers (one `git fast-import` spawn, because the brief's cost
is full-history `git log` walks that a one-commit fixture cannot show), the session's claim
dir (its `head_at_start` is the commit WINDOW below the tip, so the brief's session-bounded
walk reads a realistic window), one plan-run close-job record and one filled run-less
completion entry. A plain run then calls `workstream_complete.brief` in-process with
`stage_paths: []`. `--setup-only` does everything except the call, so the cost of the call is
`full - setup-only`.

    python -m coordinator_core.benchmarks.workstream_complete_brief_full_path <dir> [--build | --setup-only]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.win_portability import no_console_creationflags

SESSION_ID = "11111111-2222-3333-4444-555555555555"
RUN_ID = "bench-run"
PLAN_REL = "docs/plans/bench.md"
HISTORY_COMMITS = 40000  # near the real repo's 42716
WINDOW = 200  # commits between the session's head_at_start and the tip
PEER_SESSIONS = 40  # distinct peer Session-Id values cycled through history
CORPUS_DIRS = (
    ("archive/handoffs", 1400),
    ("state/handoffs", 1100),
    ("state/cross-repo/inbox", 150),
    ("state/cross-repo/archive", 300),
)
ENTRY_REL = "archive/completed/2026-10/2026-10-13-adhoc-555555.md"
_ENTRY = (
    f"---\ntitle: closed\nnature: ad-hoc\ncreated: 2026-10-13\nauthored_by: {SESSION_ID}\n---\n\n"
    "Closed without a plan run.\n"
)
_PLAN = "---\ntitle: bench\nstatus: executing\nreview_stamp: {verdict: PASS}\n---\n\n# bench\n"


def _git(repo: Path, *args: str, stdin: str | None = None) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), input=stdin and stdin.encode(), capture_output=True,
        check=True, **no_console_creationflags(),
    )


def _window_message(i: int) -> str:
    """Own 25%, untrailered 5%, peers 70% of every twenty commits."""
    slot = i % 20
    if slot == 0:
        return f"c{i}\n\nno trailer on this one\n"
    sid = SESSION_ID if slot < 6 else str(uuid.UUID(int=i % PEER_SESSIONS + 1))
    return f"c{i}\n\nSession-Id: {sid}\n"


def _corpus_files():
    """The baton and memo corpus the brief's in-process scans read, sized like the real repo's."""
    for n, (directory, count) in enumerate(CORPUS_DIRS):
        for i in range(count):
            peer = uuid.UUID(int=n * 100000 + i + 1)
            if directory.startswith("state/cross-repo"):
                body = (
                    f"---\ntitle: m{i}\npicked_up_by: {peer}\n"
                    "picked_up_at: 2026-01-01T00:00:00+00:00\n---\n\nbody\n"
                )
            else:
                claim = f"claimed_by: {peer}\n" if directory.startswith("archive") or i % 50 == 0 else ""
                body = f"---\ntitle: h{i}\n{claim}predecessor: x\n---\n\n" + "text\n" * 40
            yield f"{directory}/2026-01-01-h{i}.md", body


def _history_stream(count: int, window: int) -> str:
    parts = []
    floor = count - window
    for i in range(1, count + 1):
        in_window = i > floor
        message = (
            _window_message(i) if in_window
            else f"c{i}\n\nSession-Id: {uuid.UUID(int=i % PEER_SESSIONS + 1)}\n"
        )
        parts.append(
            f"commit refs/heads/main\nmark :{i}\n"
            f"committer t <t@example.com> {1700000000 + i} +0000\n"
            f"data {len(message.encode())}\n{message}\n"
        )
        if i == 1:
            parts.append("M 100644 inline a.txt\ndata 2\na\n\n")
            for rel, body in _corpus_files():
                parts.append(f"M 100644 inline {rel}\ndata {len(body.encode())}\n{body}\n")
            continue
        parts.append(f"from :{i - 1}\n")
        if in_window:
            if i == floor + 1:
                parts.append("R a.txt b.txt\n")
            for k in range(3):
                body = f"{i}-{k}\n" * 5
                parts.append(f"M 100644 inline src/w{i}_{k}.txt\ndata {len(body)}\n{body}\n")
    return "".join(parts)


STAMP_REL = ".git/bench-fixture-built"


def ensure_fixture(parent: Path) -> Path:
    """The fixture repo under `parent`, built once: a present completion stamp means the
    build and its verification already ran, so a measured call spawns nothing here."""
    repo = parent.resolve() / "fixture"
    stamp = repo / STAMP_REL
    if not stamp.is_file():
        if repo.exists():
            shutil.rmtree(repo)
        _build(repo)
        stamp.write_text("built", encoding="ascii")
    return repo


def _build(repo: Path) -> None:
    from coordinator_core.ops.dispatch_emit import run_close

    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    marks = repo / ".git" / "bench-marks"
    _git(
        repo, "fast-import", "--quiet", f"--export-marks={marks}",
        stdin=_history_stream(HISTORY_COMMITS, WINDOW),
    )
    floor = next(
        line.split()[1] for line in marks.read_text(encoding="ascii").splitlines()
        if line.startswith(f":{HISTORY_COMMITS - WINDOW} ")
    )
    _git(repo, "checkout", "-q", "-f", "main")
    claim = repo / ".git" / "coordinator-sessions" / SESSION_ID
    claim.mkdir(parents=True)
    (claim / "head_at_start").write_text(floor, encoding="ascii")
    (claim / "started_at").write_text(
        datetime.now().astimezone().isoformat(timespec="seconds"), encoding="ascii"
    )
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / PLAN_REL).write_text(_PLAN, encoding="utf-8")
    entry = repo / ENTRY_REL
    entry.parent.mkdir(parents=True)
    entry.write_text(_ENTRY, encoding="utf-8")
    run_close.write_close_job(
        repo, RUN_ID, (), datetime.now().astimezone() + timedelta(minutes=10),
        session_id=SESSION_ID,
        plan_rel=PLAN_REL,
        settled={
            "review": run_close.LegResult("ran"),
            "baton-drain": run_close.LegResult("ran"),
            "post-close": run_close.LegResult("ran"),
        },
    )
    _git(repo, "add", ENTRY_REL, PLAN_REL)
    _git(repo, "commit", "-q", "-m", f"entry\n\nSession-Id: {SESSION_ID}")
    above = subprocess.run(
        ["git", "rev-list", "--count", f"{floor}..HEAD"], cwd=str(repo), capture_output=True,
        text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()
    if len(floor) != 40 or above != str(WINDOW + 1):
        raise SystemExit(f"fixture floor {floor!r} has {above} commits above it, not {WINDOW + 1}")


def main(argv: list[str]) -> int:
    declare_benchmark_origin()
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a not in ("--setup-only", "--build")]
    if len(args) != 1 or unknown:
        print("usage: workstream_complete_brief_full_path <dir> [--build | --setup-only]", file=sys.stderr)
        return 2
    os.environ["COORDINATOR_SESSION_ID"] = SESSION_ID
    repo = ensure_fixture(Path(args[0]))
    if "--build" in argv:
        return 0
    from coordinator_core import workstream_complete

    if "--setup-only" in argv:
        return 0
    envelope = workstream_complete.brief({"stage_paths": []}, repo_root=repo)
    gate = (envelope.get("gates") or {}).get("run_close") or {}
    if set(gate.get("run_ids", [])) != {RUN_ID, ENTRY_REL} or not gate.get("discharged"):
        print(f"brief did not report the fixture's closes: {gate!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

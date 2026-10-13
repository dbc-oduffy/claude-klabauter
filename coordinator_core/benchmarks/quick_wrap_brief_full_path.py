"""Driver for `quick_wrap_assemble.brief(commit=False)`, measured as a process tree.

Each invocation builds a fresh repo under a caller-given directory with the history shape of
`workstream_complete_brief_full_path` (HISTORY_COMMITS commits via one `git fast-import` spawn,
the baton and memo corpora, the session's claim dir whose `head_at_start` is the commit WINDOW
below the tip), then adds what the quick-wrap brief reads on top of it: a sizing corpus at real
count with terminal records, a tracked tree at index scale, a held plan claim with its plan file,
the claim dir's `session-shape.json`, and window commits that rewrite large blobs (blob size, not
churn, is what makes the session-window walk cost real). It then calls the brief in-process.
`--setup-only` does everything except the call, so the cost of the call is `full - setup-only`.

    python -m coordinator_core.benchmarks.quick_wrap_brief_full_path <dir> [--build | --setup-only]

`--build` builds and verifies the fixture once and exits; every other invocation reuses it via
the completion stamp.
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
from coordinator_core.benchmarks.workstream_complete_brief_full_path import (
    HISTORY_COMMITS,
    PEER_SESSIONS,
    PLAN_REL,
    STAMP_REL,
    WINDOW,
    _PLAN,
    _corpus_files,
    _git,
    _window_message,
)
from coordinator_core.win_portability import no_console_creationflags

SESSION_ID = "11111111-2222-3333-4444-555555555555"
RUN_ID = "bench-run"
ENTRY_REL = "archive/completed/2026-10/2026-10-13-adhoc-555555.md"
SIZINGS = 890  # state/sizings/*.yaml on the real repo
TREE_FILES = 30000  # near the real index's 33358
SMALL_FILES_PER_COMMIT = 3
BIG_FILES_PER_COMMIT = 3
BIG_FILE_LINES = 2000  # about 100KB per rewritten blob
_ENTRY = (
    f"---\ntitle: closed\nnature: ad-hoc\ncreated: 2026-10-13\nauthored_by: {SESSION_ID}\n---\n\n"
    "Closed without a plan run.\n"
)
_SHAPE = '{"pickup": {"happened": false}, "actioned_memos": []}'


def _blob(parts: list[str], rel: str, body: str) -> None:
    parts.append(f"M 100644 inline {rel}\ndata {len(body.encode())}\n{body}\n")


def _seed_tip(parts: list[str]) -> None:
    for rel, body in _corpus_files():
        _blob(parts, rel, body)
    for k in range(SIZINGS):
        status = "shipped" if k % 4 == 0 else ("declined" if k % 17 == 0 else "sized")
        _blob(
            parts, f"state/sizings/2026-01-01-s{k}.yaml",
            f"title: s{k}\nstatus: {status}\nroute: dispatch\nplan: null\n" + "note: x\n" * 30,
        )
    for k in range(TREE_FILES):
        _blob(parts, f"tree/d{k % 200}/f{k}.txt", f"{k}\n")


def _window_blobs(parts: list[str], i: int) -> None:
    for k in range(SMALL_FILES_PER_COMMIT):
        _blob(parts, f"src/w{i}_{k}.txt", f"{i}-{k}\n" * 5)
    for k in range(BIG_FILES_PER_COMMIT):
        body = "".join(
            f"line {j} changed in {i}\n" if j < 20 else f"line {j} " + "x" * 42 + "\n"
            for j in range(BIG_FILE_LINES)
        )
        _blob(parts, f"src/big{k}.txt", body)


def _history_stream(count: int, window: int) -> str:
    parts: list[str] = []
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
            _seed_tip(parts)
            continue
        parts.append(f"from :{i - 1}\n")
        if in_window:
            if i == floor + 1:
                parts.append("R a.txt b.txt\n")
            _window_blobs(parts, i)
    return "".join(parts)


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
    sessions = repo / ".git" / "coordinator-sessions"
    claim = sessions / SESSION_ID
    claim.mkdir(parents=True)
    (claim / "head_at_start").write_text(floor, encoding="ascii")
    (claim / "started_at").write_text(
        (datetime.now().astimezone() - timedelta(seconds=60)).isoformat(timespec="seconds"),
        encoding="ascii",
    )
    (claim / "session-shape.json").write_text(_SHAPE, encoding="utf-8")
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text(_PLAN, encoding="utf-8")
    held = sessions / "plan-claims" / "bench"
    held.mkdir(parents=True)
    (held / "session_id").write_text(SESSION_ID, encoding="ascii")
    (held / "claimed_at").write_text("2026-10-13T00:00:00+00:00", encoding="ascii")
    entry = repo / ENTRY_REL
    entry.parent.mkdir(parents=True)
    entry.write_text(_ENTRY, encoding="utf-8")
    run_close.write_close_job(
        repo, RUN_ID, (), datetime.now().astimezone() + timedelta(minutes=10),
        session_id=SESSION_ID,
        settled={
            "baton-drain": run_close.LegResult("ran"),
            "orientation-refresh": run_close.LegResult("ran"),
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
        print("usage: quick_wrap_brief_full_path <dir> [--build | --setup-only]", file=sys.stderr)
        return 2
    os.environ["COORDINATOR_SESSION_ID"] = SESSION_ID
    repo = ensure_fixture(Path(args[0]))
    if "--build" in argv:
        return 0
    from coordinator_core import quick_wrap_assemble

    if "--setup-only" in argv:
        return 0
    envelope = quick_wrap_assemble.brief(repo, commit=False)
    gate = (envelope.get("gates") or {}).get("run_close") or {}
    if gate.get("close_id") != ENTRY_REL or RUN_ID not in gate.get("run_ids", []):
        print(f"brief did not report the fixture's closes: {gate!r}", file=sys.stderr)
        return 1
    if gate.get("discharged") != {"d3": RUN_ID, "d4": RUN_ID, "d5": RUN_ID}:
        print(f"brief did not discharge d3/d4/d5: {gate!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

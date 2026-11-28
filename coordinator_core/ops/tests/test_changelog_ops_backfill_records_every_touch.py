"""changelog.backfill_gaps over a >16-day range in a second repo records every written path in the session touch ledger."""

from __future__ import annotations

import asyncio
import datetime
import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.changelog_ops  # noqa: F401 -- registers changelog.backfill_gaps
from coordinator_core.ipc import dispatch_message
from coordinator_core.session import touch_record
from coordinator_core.session_hub_path import session_dir_for

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

SID = "sid-backfill-many"
FIRST_DAY = datetime.date(2026, 3, 2)
DAYS = 18


def _git(repo: Path, *args: str, stamp: str | None = None) -> None:
    env = dict(os.environ)
    if stamp:
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    return root


def _touched(repo: Path) -> list:
    sink = session_dir_for(repo / ".git", SID) / "touch-record.jsonl"
    if not sink.is_file():
        return []
    raw = sink.read_bytes()
    return [touch_record.decode_line(line) for line in touch_record.iter_complete_lines(raw)]


def test_range_backfill_into_a_second_repo_records_every_path_in_that_repos_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = _init_repo(tmp_path / "caller")
    target = _init_repo(tmp_path / "target")
    for offset in range(DAYS):
        day = FIRST_DAY + datetime.timedelta(days=offset)
        name = f"f-{day.isoformat()}.txt"
        (target / name).write_text(name, encoding="utf-8")
        _git(target, "add", name)
        _git(target, "commit", "-m", f"add {name}", stamp=f"{day.isoformat()}T12:00:00")

    monkeypatch.setenv("CLAUDE_SESSION_ID", SID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    reply = asyncio.run(
        dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "changelog.backfill_gaps",
                "params": {"from": FIRST_DAY.isoformat(), "repo": str(target)},
                "_origin_worktree": str(caller),
            }
        )
    )
    assert "error" not in reply
    result = reply["result"]
    assert "error" not in result

    written = result["backfilled"] + result["week_summaries"]
    assert len(result["backfilled"]) == DAYS
    assert len(result["week_summaries"]) == 3
    assert len(written) > 16

    root = os.path.realpath(target)
    want = sorted(
        Path(os.path.realpath(p)).relative_to(root).as_posix() for p in written
    )
    events = _touched(target)
    assert sorted(e.path for e in events) == want
    assert all(e.kind == touch_record.KIND_WRITE for e in events)

    assert _touched(caller) == []
    assert not (session_dir_for(target / ".git", SID) / "meta.json").exists()

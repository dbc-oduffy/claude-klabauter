"""
coordinator_core.ops.ceremony.tests.test_scoped_git_commit_recent_edit_warn

Spec backlink: docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-
that-rejects-it.md, chunk C1's deferred AC1d leg (C1d). Pins
`scoped_git_commit._warn_recent_edits` -- the ONLY claim-derived signal that
may exist on the commit path post-C1 -- and its hard constraint: it may WARN
(log), never gate/pause/prompt.

Gate that authorized this: C5 (05075df7d692) returned SUBSTRATE SURVIVES.
Mechanism: state/audits/2026-08-13-edit-recency-spike.md.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import scoped_git_commit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused -- `_warn_recent_edits` resolves the sessions dir via
# `claim_index.lookup(cwd=...)` -> `core.sessions_dir()`, which spawns a real
# `git rev-parse --git-common-dir` (mirrors the identical note in this
# directory's sibling `test_scoped_git_commit_claim_gate_removed.py`).
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _sessions_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions"


def _write_touched(repo: Path, sid: str, lines: list[str]) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "touched.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _write_meta(repo: Path, sid: str, *, live: bool) -> None:
    sdir = _sessions_dir(repo) / sid
    sdir.mkdir(parents=True, exist_ok=True)
    last_activity = session_core.now_iso() if live else "2020-01-01T00:00:00Z"
    (sdir / "meta.json").write_text(
        '{"pid": 1, "last_activity": "%s"}\n' % last_activity,
        encoding="utf-8",
    )


def _touch_line(verb: str, path: str, ts: str) -> str:
    return "%s %s %s" % (verb, ts, path)


_NOW = datetime(2026, 8, 13, 10, 0, 30, tzinfo=timezone.utc)


def test_edit_just_inside_window_warns(tmp_path, caplog):
    repo = _init_repo(tmp_path)
    # 30s before _NOW -- exactly at the window boundary, still inside it.
    _write_touched(
        repo, "sess-peer", [_touch_line("T", "hot.py", "2026-08-13T10:00:00.000000Z")]
    )
    _write_meta(repo, "sess-peer", live=True)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        scoped_git_commit._warn_recent_edits(
            str(repo), ["hot.py"], "sess-caller", now=_NOW
        )

    assert any("hot.py" in r.message for r in caplog.records), caplog.text
    assert any("sess-peer" in r.message for r in caplog.records), caplog.text


def test_edit_just_outside_window_is_silent(tmp_path, caplog):
    repo = _init_repo(tmp_path)
    # 31s before _NOW -- one second past the window boundary.
    _write_touched(
        repo, "sess-peer", [_touch_line("T", "cold.py", "2026-08-13T09:59:59.000000Z")]
    )
    _write_meta(repo, "sess-peer", live=True)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        scoped_git_commit._warn_recent_edits(
            str(repo), ["cold.py"], "sess-caller", now=_NOW
        )

    assert caplog.records == []


def test_touch_without_edit_inside_window_is_silent(tmp_path, caplog):
    """No `touched.txt` T-event at all -- only a bare filesystem mtime
    change -- produces no warn, however recent that mtime is. `touched.txt`
    T-events are edit-only by construction (state/audits/2026-08-13-edit-
    recency-spike.md finding 1); this pins that `_warn_recent_edits` reads
    ONLY that substrate, never disk mtime."""
    repo = _init_repo(tmp_path)
    untouched = repo / "silent.py"
    untouched.write_text("v1\n", encoding="utf-8")
    # No touched.txt entry recorded for this path at all.

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        scoped_git_commit._warn_recent_edits(
            str(repo), ["silent.py"], "sess-caller", now=_NOW
        )

    assert caplog.records == []


def test_recent_edit_by_caller_itself_is_silent(tmp_path, caplog):
    repo = _init_repo(tmp_path)
    _write_touched(
        repo, "sess-caller", [_touch_line("T", "own.py", "2026-08-13T10:00:29.000000Z")]
    )
    _write_meta(repo, "sess-caller", live=True)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        scoped_git_commit._warn_recent_edits(
            str(repo), ["own.py"], "sess-caller", now=_NOW
        )

    assert caplog.records == []


def test_recent_edit_by_dead_peer_is_silent(tmp_path, caplog):
    repo = _init_repo(tmp_path)
    _write_touched(
        repo, "sess-peer", [_touch_line("T", "hot.py", "2026-08-13T10:00:29.000000Z")]
    )
    _write_meta(repo, "sess-peer", live=False)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        scoped_git_commit._warn_recent_edits(
            str(repo), ["hot.py"], "sess-caller", now=_NOW
        )

    assert caplog.records == []


def test_recent_live_peer_edit_never_gates_the_commit(tmp_path, caplog):
    """The hard constraint: a warn-triggering scenario still commits --
    nothing in this file may pause/prompt/gate on what the warn reads.

    Review: coordinator:code-reviewer -- the prior version pinned the
    touch timestamp to 2020-01-01, always outside `_warn_recent_edits`'s
    real `datetime.now(timezone.utc)` comparison window at any wall-clock
    time this suite runs. That made the test pass vacuously: it proved an
    unclaimed/aged commit still lands (already covered by
    `test_scoped_git_commit_claim_gate_removed.py`), never that a commit
    proceeds despite a warn that ACTUALLY fired. `_handler` takes no
    `now=` override, so this pins the edit a few seconds before the real
    wall clock -- inside the 30s window at the instant `_handler` runs --
    and asserts both legs of the conjunction: the warn logged AND the
    commit landed.
    """
    repo = _init_repo(tmp_path)
    f = repo / "hot.py"
    f.write_text("v1\n", encoding="utf-8")
    _git(["add", "hot.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    f.write_text("v2\n", encoding="utf-8")

    # A few seconds before the real wall clock -- inside the 30s window
    # at the moment `_handler` below actually reads `datetime.now(...)`.
    recent_ts = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _write_touched(
        repo, "sess-peer", [_touch_line("T", "hot.py", recent_ts)]
    )
    _write_meta(repo, "sess-peer", live=True)
    _write_meta(repo, "sess-caller", live=True)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.scoped_git_commit"):
        result = scoped_git_commit._handler(
            {
                "worktree_root": str(repo),
                "paths": ["hot.py"],
                "message": "commit despite a warn-shaped scenario",
                "session_id": "sess-caller",
            },
            repo_root=None,
        )

    assert any("hot.py" in r.message for r in caplog.records), caplog.text
    assert any("sess-peer" in r.message for r in caplog.records), caplog.text
    assert result["committed"] is True, result
    assert not result.get("error"), result


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

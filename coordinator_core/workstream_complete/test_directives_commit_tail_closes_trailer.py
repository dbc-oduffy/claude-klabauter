"""test_directives_commit_tail_closes_trailer — pins AC8/AC12 for C4's
rebuilt `run_close_commit` (`directives_commit_tail.py`,
`docs/decisions/DR-358-the-close-ceremony-shape-after-the-kill.md`).

AC8: `Closes:` is composed by hand into the message body by whoever authors
the commit — `ops/emit/sections/commit_closures.py` only ever scans `git
log` for it, and no writer attaches it as a machine trailer anywhere in this
repo (verified at DR-358 authoring time). What C4 must preserve is that a
caller-composed `prose` paragraph containing a `Closes:` line survives
verbatim into the real, landed commit message — this file drives a real
commit through `run_close_commit` and reads the message back off `git log`,
never asserting against a mocked/mirrored string.

AC12: the commit route is `run_commit_pipeline` at
`push_mode=PUSH_MODE_NEVER`, never `git_native.commit_scoped` directly, and
a test asserts the mode actually passed — this file asserts it two ways: by
spying on the exact kwarg `run_commit_pipeline` receives, and by observing
that a real close never attempts a push (no push-status artifact, no
network call) even though this fixture never configures a remote to fail
against.

Real, throwaway `tmp_path` git repo only — mirrors
`coordinator_core/ops/ceremony/tests/test_commit_push_mode_default.py`'s own
established shape for driving `run_commit_pipeline` end to end. Never this
repo, never a `git init` scratch fixture standing in for a performance claim
(this file makes no timing assertion at all — hard constraint 2 governs
*measurement* chunks, not this functional pin).

Run: python3 -m pytest coordinator_core/workstream_complete/test_directives_commit_tail_closes_trailer.py -q -p no:randomly
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.push import (
    PUSH_MODE_NEVER,
    PUSH_STATUS_NOT_ATTEMPTED,
)
from coordinator_core.workstream_complete import directives_commit_tail as _tail
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags())


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _unique_session_id() -> str:
    return f"test-session-{uuid.uuid4().hex[:8]}"


def _commit_message(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    **no_console_creationflags(),
)
    return proc.stdout


def test_closes_line_survives_verbatim_into_the_real_commit(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "workstream_note.md").write_text("content", encoding="utf-8")

    prose = "Closes: dlv-example-close-verbatim-01"
    result = _tail.run_close_commit(
        repo,
        session_id=_unique_session_id(),
        subject="close a session through the rebuilt commit step",
        prose=prose,
        stage_paths=["workstream_note.md"],
        caller_paths={"workstream_note.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.committed_sha is not None

    message = _commit_message(repo)
    assert "Closes: dlv-example-close-verbatim-01" in message


def test_multiline_prose_with_closes_line_survives_verbatim(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "workstream_note2.md").write_text("content", encoding="utf-8")

    prose = (
        "First paragraph of hand-authored prose.\n\n"
        "Closes: dlv-example-close-multiline-02"
    )
    result = _tail.run_close_commit(
        repo,
        session_id=_unique_session_id(),
        subject="close a session with multi-paragraph prose",
        prose=prose,
        stage_paths=["workstream_note2.md"],
        caller_paths={"workstream_note2.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    message = _commit_message(repo)
    assert "First paragraph of hand-authored prose." in message
    assert "Closes: dlv-example-close-multiline-02" in message


# `test_push_mode_never_is_the_kwarg_run_commit_pipeline_actually_receives`
# (deleted, C4 of docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-
# the-pipeline-can-go.md): it monkeypatched `commit_pipeline.
# run_commit_pipeline`, a call `run_close_commit` no longer makes --
# `directives_commit_tail.py` was already repointed onto
# `coordinator_core.git.commit.commit_paths` by an earlier chunk, so the spy
# never fired (pre-existing failure at this chunk's dispatch: `KeyError:
# 'push_mode'`, not something this chunk introduced). The sibling test below,
# `test_a_real_close_never_pushes_and_never_touches_a_remote`, already covers
# the same behavioural claim (`PUSH_MODE_NEVER`'s effect) via real state
# observation rather than a spy on a dead call path.


def test_a_real_close_never_pushes_and_never_touches_a_remote(tmp_path):
    """Complements the spy above with a state-observation check: a real
    close, with no remote configured at all, must not fail or behave
    differently than it would with one — `PUSH_MODE_NEVER` means the push
    leg is never attempted, not merely declined after an attempt."""
    repo = _init_repo(tmp_path)
    (repo / "workstream_note3.md").write_text("content", encoding="utf-8")

    result = _tail.run_close_commit(
        repo,
        session_id=_unique_session_id(),
        subject="never pushes",
        stage_paths=["workstream_note3.md"],
        caller_paths={"workstream_note3.md"},
    )

    assert result.commit_failed is False, result.diagnostics
    assert result.pushed is None
    assert result.push_status == PUSH_STATUS_NOT_ATTEMPTED

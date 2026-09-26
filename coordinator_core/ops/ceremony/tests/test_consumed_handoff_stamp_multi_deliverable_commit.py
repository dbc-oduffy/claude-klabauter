
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.push import PUSH_MODE_NONE
from coordinator_core.ops.ceremony.consumed_handoff_stamp import (
    _commit_and_push_follow_up,
    group_stamped_by_deliverable_id,
)
from coordinator_core.win_portability import no_console_creationflags

# `_BASELINE` is shrink-only and explicitly not the route for a new file
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SID = "11111111-2222-3333-4444-555555555555"


def _handoff(deliverable_id: str) -> str:
    return "\n".join(["---", "kind: handoff", f"deliverable_id: {deliverable_id}", "---", "", "body", ""])


def _write(root: Path, rel: str, deliverable_id: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_handoff(deliverable_id), encoding="utf-8")
    return rel


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


@pytest.fixture
def sid_env(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    return _SID


def test_ungrouped_two_baton_pathspec_commits_untrailered(repo, sid_env):
    """Inverted by DR-406 (2026-08-19). This test formerly asserted that an
    ungrouped two-baton pathspec RAISES `DivergentDeliverableIdError`, and its
    docstring warned that "a future change that relaxed the resolver into
    guessing would make the grouping look unnecessary while silently
    mis-attributing every multi-baton commit".

    That warning was right about guessing and wrong about the only alternative.
    DR-406's C2 did not relax the resolver into guessing -- it relaxed it into
    OMITTING, per producer-contract § 3: on a divergent pathspec the commit
    lands carrying no `Deliverable-Id:` trailer at all, so nothing is
    mis-attributed. The commit is then attributed via `Session-Id:`, which C9
    measured against real history and found loses no rows
    (`state/audits/2026-08-19-multi-deliverable-commit-attribution.md`).

    The grouping this test defended IS now unnecessary, and C10 retired its
    production call site deliberately rather than by accident -- which is the
    distinction the original docstring existed to force someone to make.
    """
    a = _write(repo, "state/handoffs/a.md", "dlv-alpha-000001")
    b = _write(repo, "state/handoffs/b.md", "dlv-beta-000002")

    sha, _pushed, _status, error = _commit_and_push_follow_up(
        repo, [a, b], "deadbeef", PUSH_MODE_NONE, sid_env
    )

    assert error is None, error
    assert sha is not None
    msg = subprocess.run(
        ["git", "log", "-1", "--format=%B", sha],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout
    assert "Deliverable-Id:" not in msg, (
        "a pathspec spanning two deliverables must omit the trailer, never "
        f"guess a winner; got:\n{msg}"
    )


def test_each_group_commits_cleanly_with_its_own_trailer(repo, sid_env):
    a = _write(repo, "state/handoffs/a.md", "dlv-alpha-000001")
    b = _write(repo, "state/handoffs/b.md", "dlv-beta-000002")

    landed = []
    for deliverable_id, paths in group_stamped_by_deliverable_id(repo, [a, b]):
        sha, _pushed, _status, error = _commit_and_push_follow_up(
            repo, paths, "deadbeef", PUSH_MODE_NONE, sid_env
        )
        assert error is None, f"{deliverable_id}: {error}"
        assert sha is not None
        landed.append((deliverable_id, sha))

    assert len(landed) == 2
    assert len({sha for _d, sha in landed}) == 2

    for deliverable_id, sha in landed:
        message = subprocess.run(
            ["git", "show", "-s", "--format=%B", sha],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
            **no_console_creationflags(),
        ).stdout
        assert f"Deliverable-Id: {deliverable_id}" in message

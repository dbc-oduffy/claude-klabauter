"""
coordinator_core.ops.ceremony.tests.test_consumed_handoff_stamp_multi_deliverable_commit

Purpose: the git-backed half of the multi-baton follow-up-commit contract --
`test_consumed_handoff_stamp_multi_deliverable.py` covers the grouping itself
(pure frontmatter reads, fast tier); this file drives the real commit leg
through real git, because the property under test IS the trailer resolution
`git_native.commit_scoped` performs at commit time. A mocked git resolves no
trailers and would assert nothing.

Bug closed: `state/bug-backlog/2026-08-14-wsc-tail-cannot-stamp-a-two-baton-
pickup.yaml` -- see the grouping file's module docstring for the full defect.

Split from that file, rather than merged into it, so its three grouping tests
stay in the fast tier: `pytestmark` is module-scoped and marking this file's
git-spawning helpers (which the spawn ratchet reads at module level -- Rule 2,
`coordinator_core/tests/test_no_new_spawning_tests.py`) would drag the pure
tests into `cadence` with them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.commit_pipeline import PUSH_MODE_NONE
from coordinator_core.ops.ceremony.consumed_handoff_stamp import (
    _commit_and_push_follow_up,
    group_stamped_by_deliverable_id,
)

# Real git, per the docstring above. Same tiering rationale as the sibling
# `test_consumed_handoff_stamp_claim_release.py`; the spawn ratchet's
# `_BASELINE` is shrink-only and explicitly not the route for a new file
# (coordinator_core/tests/test_no_new_spawning_tests.py Rule 2).
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

#: Trailer resolution abstains entirely on a non-UUID session id (see
#: `commit_trailers.compute_missing_trailer_args`'s fail-safe), so tier 0 --
#: the tier this contract turns on -- is only reachable with a UUID-shaped id.
_SID = "11111111-2222-3333-4444-555555555555"


def _handoff(deliverable_id: str) -> str:
    return "\n".join(["---", "kind: handoff", f"deliverable_id: {deliverable_id}", "---", "", "body", ""])


def _write(root: Path, rel: str, deliverable_id: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_handoff(deliverable_id), encoding="utf-8")
    return rel


def _git(args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


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
    """Inverted by DR-328 (2026-08-19). This test formerly asserted that an
    ungrouped two-baton pathspec RAISES `DivergentDeliverableIdError`, and its
    docstring warned that "a future change that relaxed the resolver into
    guessing would make the grouping look unnecessary while silently
    mis-attributing every multi-baton commit".

    That warning was right about guessing and wrong about the only alternative.
    DR-328's C2 did not relax the resolver into guessing -- it relaxed it into
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
    ).stdout
    assert "Deliverable-Id:" not in msg, (
        "a pathspec spanning two deliverables must omit the trailer, never "
        f"guess a winner; got:\n{msg}"
    )


def test_each_group_commits_cleanly_with_its_own_trailer(repo, sid_env):
    """The fix's payload: committing group-by-group lands one commit per
    deliverable, each carrying that deliverable's own `Deliverable-Id:`
    trailer -- the close a two-baton pickup could not previously complete."""
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
        ).stdout
        assert f"Deliverable-Id: {deliverable_id}" in message

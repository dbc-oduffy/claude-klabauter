"""`ceremony.commit_v2` can preserve a peer's staged blob without naming it.

`commit_paths` has taken `prefer_deliberate_stage` since DR-379 and
`ops/session/safe_commit_offer.py` has passed it, but `commit_v2` did not
plumb it -- so a caller of the OP had `prefer_staged` (which requires knowing
the diverging paths up front) or nothing. On a shared branch you cannot know
them up front: the divergence is a peer's uncommitted work, appearing between
your read and your commit.

That gap is what example-game-repo-em lost attribution to on 2026-09-01 (memo
`example-game-repo-em-close-ceremony-engine-defects-seven`, defect 6) -- a peer's
whoami.ts and index.ts hunks landed under their authorship in a089fdbe2, not
correctable afterwards because reverting a hunk you did not write is
forbidden. Their own remedy was to abandon the op for a hand-rolled
`git commit -F <msg> -- <paths>`, which costs the op's branch-gate and policy
legs.

NEGATIVE SPEC: the default is NOT flipped. Worktree-preference remains what an
undeclared call does -- flipping it is a fleet-visible behaviour change and is
the PM's, not this seam's. `test_default_is_still_worktree_preference` is the
half that pins that, and it must not be relaxed into a smoke test of the flag.

Throwaway `tmp_path` repos throughout; the working repo is never touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "peer.md").write_bytes(b"seed\n")
    _git(["add", "--", "peer.md"], repo)
    _git(["commit", "-qm", "seed"], repo)
    return repo


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _diverge(repo: Path) -> None:
    """A peer's deliberate partial stage: staged bytes, then a further
    worktree edit on the same path -- the exact shape `worktree_over_staged`
    reports and the one a shared branch produces."""
    (repo / "peer.md").write_bytes(b"peer staged this\n")
    _git(["add", "--", "peer.md"], repo)
    (repo / "peer.md").write_bytes(b"and then the worktree moved on\n")


def _blob_at_head(repo: Path, path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    ).stdout


def test_prefer_deliberate_stage_commits_the_staged_blob(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _diverge(repo)

    result = _call(repo, {
        "paths": ["peer.md"],
        "message": "preserve the peer's staged bytes",
        "prefer_deliberate_stage": True,
    })

    assert result["committed"] is True, result
    assert _blob_at_head(repo, "peer.md") == b"peer staged this\n"
    # Preserved, therefore NOT passed over: the path moves into
    # `staged_preferred` and out of the divergence report, so the operator is
    # not warned about a substitution they asked for.
    assert result["staged_preferred"] == ["peer.md"]
    assert result["worktree_over_staged"] == []


def test_default_is_still_worktree_preference(tmp_path: Path) -> None:
    """The negative spec. An undeclared call behaves exactly as before --
    worktree bytes land and the divergence is REPORTED, not silently taken."""
    repo = _repo(tmp_path)
    _diverge(repo)

    result = _call(repo, {
        "paths": ["peer.md"],
        "message": "undeclared call",
    })

    assert result["committed"] is True, result
    assert _blob_at_head(repo, "peer.md") == b"and then the worktree moved on\n"
    assert result["worktree_over_staged"] == ["peer.md"]
    assert result["staged_preferred"] == []


def test_the_divergence_warning_names_the_shared_branch_remedy(tmp_path: Path) -> None:
    """A warning naming only `prefer_staged` is unactionable on a shared
    branch, where naming the paths up front is the thing you cannot do."""
    repo = _repo(tmp_path)
    _diverge(repo)

    result = _call(repo, {"paths": ["peer.md"], "message": "undeclared call"})

    warning = "\n".join(result["warnings"])
    assert "prefer_deliberate_stage" in warning
    assert "peer.md" in warning


def test_a_non_boolean_flag_is_refused_not_coerced(tmp_path: Path) -> None:
    """`"false"` is truthy. A flag deciding whose bytes land must refuse the
    string rather than silently read it as True."""
    repo = _repo(tmp_path)

    result = _call(repo, {
        "paths": ["peer.md"],
        "message": "bad flag type",
        "prefer_deliberate_stage": "false",
    })

    assert result["committed"] is False
    assert "prefer_deliberate_stage" in result["error"]

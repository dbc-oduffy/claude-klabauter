"""`ceremony.commit_v2` forwards `prefer_deliberate_stage` to `commit_paths`.

The SEMANTICS of that flag are `commit_paths`' and are pinned there
(`git/tests/test_action_guard_default_path.py`, legs (a) and (b)). What
commit_v2 added is plumbing, so that is what these assert -- re-running the
three-blob fixture here would pay a real `git init` to re-prove someone else's
contract.

Reported by example-game-repo-em 2026-09-01 (memo
`example-game-repo-em-close-ceremony-engine-defects-seven`, defect 6): the mechanism
existed and `safe_commit_offer` passed it, but callers of the OP had
`prefer_staged` -- which needs the diverging paths up front -- or nothing.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony import commit_v2


def _spy(monkeypatch):
    """Capture the kwargs commit_v2 hands `commit_paths`, and stop there."""
    seen: dict = {}

    def fake_commit_paths(*args, **kwargs):
        seen.update(kwargs)
        raise AssertionError("stop-after-capture")

    monkeypatch.setattr(commit_v2, "commit_paths", fake_commit_paths)
    return seen


def _call(repo_root, params):
    return commit_v2._handler(params, repo_root=repo_root)


def test_the_flag_reaches_commit_paths(monkeypatch, tmp_path):
    seen = _spy(monkeypatch)
    with pytest.raises(AssertionError, match="stop-after-capture"):
        _call(tmp_path / ".git", {
            "paths": ["a.md"],
            "message": "m",
            "prefer_deliberate_stage": True,
        })
    assert seen["prefer_deliberate_stage"] is True


def test_the_default_is_false_when_undeclared(monkeypatch, tmp_path):
    """The negative spec. Worktree-preference stays what an undeclared call
    does; flipping it is a fleet-visible change and not this seam's."""
    seen = _spy(monkeypatch)
    with pytest.raises(AssertionError, match="stop-after-capture"):
        _call(tmp_path / ".git", {"paths": ["a.md"], "message": "m"})
    assert seen["prefer_deliberate_stage"] is False


def test_both_declarations_travel_together(monkeypatch, tmp_path):
    """`prefer_staged` and `prefer_deliberate_stage` are not exclusive --
    a `prefer_staged` path is settled before the loop the blanket flag walks,
    so they cannot both act on one path. Pinned because the seam forwards
    both and nothing else asserts they survive the same call."""
    seen = _spy(monkeypatch)
    with pytest.raises(AssertionError, match="stop-after-capture"):
        _call(tmp_path / ".git", {
            "paths": ["a.md", "b.md"],
            "message": "m",
            "prefer_staged": ["a.md"],
            "prefer_deliberate_stage": True,
        })
    assert seen["prefer_staged"] == ["a.md"]
    assert seen["prefer_deliberate_stage"] is True


def test_a_non_boolean_flag_is_refused_not_coerced(tmp_path):
    """`"false"` is truthy. A flag deciding whose bytes land must refuse the
    string rather than read it as True. `isinstance(1, bool)` is False, so a
    JSON-RPC-delivered int is refused here too."""
    result = _call(tmp_path / ".git", {
        "paths": ["a.md"],
        "message": "m",
        "prefer_deliberate_stage": "false",
    })
    assert result["committed"] is False
    assert "prefer_deliberate_stage" in result["error"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_divergence_warning_names_the_shared_branch_remedy(tmp_path):
    """The one real-git case, and it is this seam's own output. A warning
    naming only `prefer_staged` is unactionable on a shared branch, where
    naming the paths up front is the thing you cannot do -- which is what
    example-game-repo-em was told before doing the wrong thing about it. Asserted by
    provoking a real divergence, not by grepping the source for the literal:
    a string that exists but is never emitted would pass that and fail here.
    """
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    def git(*args):
        subprocess.run(["git", *args], cwd=str(repo), check=True,
                       capture_output=True, **no_console_creationflags())

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q")
    git("config", "user.email", "t@t.example")
    git("config", "user.name", "t")
    (repo / "peer.md").write_bytes(b"seed\n")
    git("add", "--", "peer.md")
    git("commit", "-qm", "seed")

    # A peer's deliberate partial stage: staged bytes, then a further
    # worktree edit on the same path.
    (repo / "peer.md").write_bytes(b"peer staged this\n")
    git("add", "--", "peer.md")
    (repo / "peer.md").write_bytes(b"and then the worktree moved on\n")

    result = commit_v2._handler(
        {"paths": ["peer.md"], "message": "undeclared call"},
        repo_root=repo / ".git",
    )

    assert result["committed"] is True, result
    assert result["worktree_over_staged"] == ["peer.md"]
    warning = "\n".join(result["warnings"])
    assert "prefer_deliberate_stage" in warning
    assert "peer.md" in warning

"""The doctrine-edit denial names the absolute git dir of the session cwd and a human ``!`` touch of the approval marker."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import _doctrine_edit_sentinel as sentinel
from coordinator_core.write_guards import guard_doctrine_surface_edits as guard


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".claude" / "rules").mkdir(parents=True)
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("# doctrine\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(guard, "_git_root", lambda: str(root))
    monkeypatch.setattr(guard, "read_content_root", lambda: None)
    monkeypatch.setattr(sentinel, "read_content_root", lambda: None)
    monkeypatch.setattr(guard, "gem_admitted", lambda *a, **k: False)
    monkeypatch.setattr(guard, "feature_enabled", lambda name: True)
    monkeypatch.setattr(
        guard, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "MATCH", "message": ""}
    )
    monkeypatch.setattr(guard, "_write_repo_identity_advisory_log", lambda *a, **k: None)
    guard._content_root_memo.clear()
    yield root
    guard._content_root_memo.clear()


def _edit(root: Path, cwd: Path) -> dict:
    return {
        "tool_name": "Edit",
        "session_id": "sess-1",
        "cwd": str(cwd),
        "tool_input": {
            "file_path": str(root / "CLAUDE.md"),
            "old_string": "doctrine",
            "new_string": "rule",
        },
    }


def test_denial_names_absolute_git_dir_and_human_touch(repo: Path) -> None:
    out = guard.check(_edit(repo, repo))["hookSpecificOutput"]
    reason = out["permissionDecisionReason"]
    marker = (repo / ".git" / sentinel.SENTINEL_BASENAME).resolve().as_posix()
    assert out["permissionDecision"] == "deny"
    assert sentinel.DENY_REASON in reason
    assert f'! touch "{repo.joinpath(".git", sentinel.SENTINEL_BASENAME).as_posix()}"' in reason
    assert marker.lower() in reason.lower()


def test_denial_resolves_git_dir_from_a_subdirectory_cwd(repo: Path) -> None:
    sub = repo / "pkg" / "inner"
    sub.mkdir(parents=True)
    reason = guard.check(_edit(repo, sub))["hookSpecificOutput"]["permissionDecisionReason"]
    assert (repo / ".git" / sentinel.SENTINEL_BASENAME).as_posix() in reason


def test_deny_reason_without_a_git_dir_is_the_bare_reason(tmp_path: Path) -> None:
    bare = tmp_path / "nogit"
    bare.mkdir()
    if sentinel.git_dir_for(str(bare)) is not None:
        pytest.skip("tmp_path sits inside a git checkout")
    assert sentinel.deny_reason(str(bare)) == sentinel.DENY_REASON


def test_deny_reason_follows_a_gitdir_file(tmp_path: Path) -> None:
    real = tmp_path / "real-gitdir"
    real.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {real.as_posix()}\n", encoding="utf-8")
    reason = sentinel.deny_reason(str(wt))
    assert f'! touch "{(real / sentinel.SENTINEL_BASENAME).as_posix()}"' in reason


def test_marker_is_looked_up_from_the_target_repo_not_the_session_cwd(tmp_path: Path) -> None:
    """Friction #42: a session outside any repo is admitted by the marker in the target file's repo."""
    target_repo = tmp_path / "dot-claude"
    (target_repo / ".git").mkdir(parents=True)
    outside = tmp_path / "desktop"
    outside.mkdir()
    target = str(target_repo / "CLAUDE.md")
    if sentinel.git_dir_for(str(outside)) is not None:
        pytest.skip("tmp_path sits inside a git checkout")
    assert not sentinel.sentinel_present(str(outside), (target,))
    marker = target_repo / ".git" / sentinel.SENTINEL_BASENAME
    assert f'! touch "{marker.as_posix()}"' in sentinel.deny_reason(str(outside), (target,))
    marker.write_text("", encoding="utf-8")
    assert sentinel.sentinel_present(str(outside), (target,))
    assert not sentinel.sentinel_present(str(outside))

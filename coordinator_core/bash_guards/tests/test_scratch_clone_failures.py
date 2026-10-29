"""scratch_clone_failures with an injected git runner: UNREADABLE on rc != 0 or a raised OSError,
[] for a clean repo."""

from __future__ import annotations

import subprocess

from coordinator_core.bash_guards._unborn_scratch_repo import (
    UNPUSHED,
    UNREADABLE,
    scratch_clone_failures,
)


def _scratch_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    clone = tmp_path / "scratch" / "c"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return str(clone)


def test_nonzero_rc_is_unreadable(tmp_path):
    root = _scratch_repo(tmp_path)
    assert scratch_clone_failures(root, lambda args, cwd: (128, "")) == [UNREADABLE]


def test_raised_oserror_is_unreadable(tmp_path):
    root = _scratch_repo(tmp_path)

    def boom(args, cwd):
        raise OSError("spawn failed")

    assert scratch_clone_failures(root, boom) == [UNREADABLE]


def test_clean_repo_has_no_failures(tmp_path):
    root = _scratch_repo(tmp_path)
    assert scratch_clone_failures(root, lambda args, cwd: (0, "")) == []


def _real_git(args, cwd):
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.returncode, out.stdout


def _g(cwd, *args):
    rc, out = _real_git(
        ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "-c", "tag.gpgsign=false", *args], str(cwd))
    assert rc == 0, args
    return out.strip()


def _origin_and_clone(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _g(origin, "init", "-q", "-b", "main")
    _g(origin, "commit", "-q", "--allow-empty", "-m", "one")
    (tmp_path / ".git").mkdir()
    clone = tmp_path / "scratch" / "c"
    clone.parent.mkdir()
    _g(tmp_path, "clone", "-q", str(origin), str(clone))
    return origin, clone


def test_detached_head_at_pushed_commit_is_recoverable(tmp_path):
    _, clone = _origin_and_clone(tmp_path)
    _g(clone, "checkout", "-q", "--detach", "HEAD")
    assert scratch_clone_failures(str(clone), _real_git) == []


def test_local_tag_on_remote_but_not_under_remotes_is_recoverable(tmp_path):
    origin, clone = _origin_and_clone(tmp_path)
    _g(clone, "checkout", "-q", "--detach", "HEAD")
    _g(clone, "commit", "-q", "--allow-empty", "-m", "side")
    _g(clone, "tag", "backup/x")
    _g(clone, "push", "-q", "origin", "refs/tags/backup/x")
    _g(clone, "checkout", "-q", "main")
    assert _g(clone, "rev-list", "--max-count=1", "--tags", "--not", "--remotes")
    assert scratch_clone_failures(str(clone), _real_git) == []


def test_unpushed_commit_stays_unpushed(tmp_path):
    _, clone = _origin_and_clone(tmp_path)
    _g(clone, "commit", "-q", "--allow-empty", "-m", "local")
    assert scratch_clone_failures(str(clone), _real_git) == [UNPUSHED]


def test_unpushed_tag_stays_unpushed(tmp_path):
    _, clone = _origin_and_clone(tmp_path)
    _g(clone, "checkout", "-q", "--detach", "HEAD")
    _g(clone, "commit", "-q", "--allow-empty", "-m", "side")
    _g(clone, "tag", "lost")
    _g(clone, "checkout", "-q", "main")
    assert scratch_clone_failures(str(clone), _real_git) == [UNPUSHED]


def test_annotated_tag_on_remote_is_recoverable(tmp_path):
    _, clone = _origin_and_clone(tmp_path)
    _g(clone, "checkout", "-q", "--detach", "HEAD")
    _g(clone, "commit", "-q", "--allow-empty", "-m", "side")
    _g(clone, "tag", "-a", "-m", "ann", "backup/ann")
    _g(clone, "push", "-q", "origin", "refs/tags/backup/ann")
    _g(clone, "checkout", "-q", "main")
    assert scratch_clone_failures(str(clone), _real_git) == []


def test_unreachable_origin_stays_unpushed(tmp_path):
    _, clone = _origin_and_clone(tmp_path)
    _g(clone, "checkout", "-q", "--detach", "HEAD")
    _g(clone, "commit", "-q", "--allow-empty", "-m", "side")
    _g(clone, "tag", "lost")
    _g(clone, "checkout", "-q", "main")
    _g(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))
    assert scratch_clone_failures(str(clone), _real_git) == [UNPUSHED]


def _clone_verdict(cwd):
    from coordinator_core.bash_guards import block_subagent_destructive_action as guard

    result = guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git clone https://example.invalid/x.git /elsewhere/x"},
            "session_id": "sess1",
            "cwd": str(cwd),
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:queue-grind-op-runner",
        }
    )
    return None if result is None else result.summary


def test_clone_denial_outside_any_repo_names_no_placeholder(tmp_path):
    verdict = _clone_verdict(tmp_path)
    assert verdict is not None
    assert "<repo>" not in verdict
    assert "scratchpad" in verdict


def test_clone_denial_inside_repo_names_resolved_scratch(tmp_path):
    (tmp_path / ".git").mkdir()
    verdict = _clone_verdict(tmp_path)
    assert verdict is not None
    assert "<repo>" not in verdict
    assert (tmp_path.resolve() / "scratch").as_posix() in verdict

"""Tests for coordinator_core.bash_guards.block_topic_branch.

Pure parsing: a fake `.git` dir under tmp_path supplies HEAD and config, and
subprocess spawning is made fatal to prove the path spawns nothing.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.bash_guards import block_topic_branch as guard

KEY = "COORDINATOR_OVERRIDE_TOPIC_BRANCH"
DAY = "work/machine-a/2026-10-04"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(f"ref: refs/heads/{DAY}\n", encoding="utf-8")
    monkeypatch.setattr(guard, "_machine", lambda: "machine-a")

    def _no_spawn(*a, **k):
        raise AssertionError("guard spawned a process")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    return tmp_path


def _check(repo, command, env=None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    payload["env"] = env if env is not None else {}
    return guard.check(payload)


def _denied(out):
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "cmd",
    [
        "git checkout -b fix/foo",
        "git checkout -B fix/foo",
        "git switch -c fix/foo",
        "git switch -C fix/foo",
        "git switch --create fix/foo",
        "git branch fix/foo",
        "git branch fix/foo main",
        "git push origin fix/foo",
        "git push -u origin fix/foo",
        "git push origin HEAD:fix/foo",
        "git push origin HEAD:refs/heads/fix/foo",
        "git push origin +main:fix/foo",
        "git -C . checkout -b fix/foo",
        "git -C sub -c core.x=1 push origin fix/foo",
        "cd x && git checkout -b fix/foo",
        "git checkout -b work/otherbox/2026-10-05",
        "git push origin work/machine-a/2026-10-05-closeout",
        "git push -q origin fix/foo 2>&1",
        "git push origin fix/foo > out.txt",
        "git push origin fix/foo 2>/dev/null | tail -1",
    ],
)
def test_denies(repo, cmd):
    out = _check(repo, cmd)
    assert _denied(out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert KEY in reason and "Group EM" in reason and "day branch" in reason


@pytest.mark.parametrize(
    "cmd",
    [
        "git push origin main",
        "git push origin HEAD:main",
        "git push",
        "git push origin",
        "git push origin HEAD",
        f"git push origin {DAY}",
        "git push origin work/machine-a/2026-10-05",
        "git checkout -b work/machine-a/2026-10-05",
        "git switch -c work/machine-a/2026-10-05to07",
        "git branch work/machine-a/2026-10-05",
        "git push origin --delete fix/foo",
        "git push origin :fix/foo",
        "git push origin --tags",
        "git push origin refs/tags/v1.2.3",
        "git branch",
        "git branch -a",
        "git branch -v",
        "git branch --list",
        "git branch --show-current",
        "git branch -d fix/foo",
        "git branch -D fix/foo",
        "git branch -m old new",
        "git checkout fix/foo",
        "git switch main",
        "git checkout -b \"$BRANCH\"",
        "git status",
        "git push -q origin work/machine-a/2026-10-05 2>&1",
        "git push origin work/machine-a/2026-10-05 > out.txt 2>&1",
        "git push origin work/machine-a/2026-10-05 2> err.txt",
        "git push origin work/machine-a/2026-10-05 &>log",
        "git push origin main 2>/dev/null | tail -1",
        "git push origin main && echo done",
    ],
)
def test_allows(repo, cmd):
    assert _check(repo, cmd) is None


def _mirror(monkeypatch, root):
    from coordinator_core import engine_root

    monkeypatch.setattr(
        engine_root, "is_published_engine_mirror", lambda r: str(r) == str(root)
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "git push origin candidate",
        "git push -u origin candidate",
        "git push origin HEAD:candidate",
        "git push origin HEAD:refs/heads/candidate",
    ],
)
def test_publish_branch_push_allowed_in_published_mirror(repo, monkeypatch, cmd):
    _mirror(monkeypatch, repo)
    assert _check(repo, cmd) is None


def test_publish_branch_push_via_dash_c_into_mirror(repo, tmp_path_factory, monkeypatch):
    _mirror(monkeypatch, repo)
    other = tmp_path_factory.mktemp("cwd")
    assert _check(other, f"git -C {repo} push origin candidate") is None


@pytest.mark.parametrize("sep", [" && ", "; "])
def test_publish_branch_push_after_cd_into_mirror_judged_like_dash_c(repo, tmp_path_factory, monkeypatch, sep):
    _mirror(monkeypatch, repo)
    other = tmp_path_factory.mktemp("cwd")
    assert _check(other, f"cd {repo}{sep}git push origin candidate") is None
    assert _denied(_check(other, f"cd {repo}{sep}git push origin candidate-2"))


def test_cd_inside_a_subshell_does_not_move_the_judged_repo(repo, tmp_path_factory, monkeypatch):
    _mirror(monkeypatch, repo)
    other = tmp_path_factory.mktemp("cwd")
    assert _denied(_check(other, f"(cd {repo} && true); git push origin candidate"))


def test_a_quoted_paren_is_not_a_subshell(repo, tmp_path_factory, monkeypatch):
    _mirror(monkeypatch, repo)
    other = tmp_path_factory.mktemp("cwd")
    smoke = "python -c \"import x\nprint('ok')\""
    assert _check(other, f"cd {repo} && {smoke} && git push origin candidate") is None


def test_publish_branch_push_still_denied_outside_mirror(repo, monkeypatch):
    _mirror(monkeypatch, repo / "elsewhere")
    assert _denied(_check(repo, "git push origin candidate"))


def test_other_topic_refs_still_denied_in_published_mirror(repo, monkeypatch):
    _mirror(monkeypatch, repo)
    assert _denied(_check(repo, "git push origin fix/foo"))
    assert _denied(_check(repo, "git push origin candidate-2"))
    assert _denied(_check(repo, "git checkout -b candidate"))
    assert _denied(_check(repo, "git branch candidate"))


def test_push_head_denied_when_head_is_a_topic_branch(repo):
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/fix/foo\n", encoding="utf-8")
    assert _denied(_check(repo, "git push origin HEAD"))


def test_git_tag_never_denied(repo):
    assert not _denied(_check(repo, 'git tag -a v4.4.9 abc -m x'))


def test_push_existing_local_tag_allowed(repo):
    tags = repo / ".git" / "refs" / "tags"
    tags.mkdir(parents=True)
    (tags / "v4.4.9").write_text("abc\n", encoding="utf-8")
    assert not _denied(_check(repo, "git push origin v4.4.9"))


def test_push_packed_local_tag_allowed(repo):
    (repo / ".git" / "packed-refs").write_text("abc123 refs/tags/v5.0\n", encoding="utf-8")
    assert not _denied(_check(repo, "git push origin v5.0"))


@pytest.mark.parametrize(
    "cmd", ["git push origin refs/tags/v4.4.9", "git push origin --tags", "git push origin tag v1"]
)
def test_tag_pushes_allowed(repo, cmd):
    assert not _denied(_check(repo, cmd))


def test_push_unknown_topic_branch_still_denied_with_tags_present(repo):
    tags = repo / ".git" / "refs" / "tags"
    tags.mkdir(parents=True)
    (tags / "v4.4.9").write_text("abc\n", encoding="utf-8")
    assert _denied(_check(repo, "git push origin feature-x"))


def test_configured_day_branch_is_allowed(repo):
    (repo / ".git" / "config").write_text(
        "[coordinator]\n\tdayBranch = harness/cloud-1\n", encoding="utf-8"
    )
    assert _check(repo, "git push origin harness/cloud-1") is None
    assert _denied(_check(repo, "git push origin harness/other"))


def test_inline_override_with_reason_allows(repo):
    assert _check(repo, f"{KEY}=groupem-approved git checkout -b fix/foo") is None
    assert _check(repo, f'{KEY}="group em ok" git push origin fix/foo') is None


def test_inline_override_applies_to_its_own_segment_only(repo):
    out = _check(repo, f"{KEY}=why git checkout -b a/b && git checkout -b c/d")
    assert _denied(out)
    assert "c/d" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("prefix", [f"{KEY}=", f'{KEY}=""', f"{KEY}=   "])
def test_empty_override_still_denies(repo, prefix):
    assert _denied(_check(repo, f"{prefix} git checkout -b fix/foo"))


def test_env_var_alone_never_overrides(repo, monkeypatch):
    assert _denied(_check(repo, "git checkout -b fix/foo", env={KEY: "why"}))
    monkeypatch.setenv(KEY, "why")
    assert _denied(_check(repo, "git checkout -b fix/foo", env=None))


@pytest.mark.parametrize("cmd", ["ls -la", "python -m pytest x.py", "echo hi && cat f"])
def test_non_git_commands_spawn_nothing_and_skip_machine_lookup(repo, monkeypatch, cmd):
    def _boom():
        raise AssertionError("machine lookup ran for a non-git command")

    monkeypatch.setattr(guard, "_machine", _boom)
    assert _check(repo, cmd) is None


def test_main_and_nonbranch_git_skip_machine_lookup(repo, monkeypatch):
    def _boom():
        raise AssertionError("machine lookup ran")

    monkeypatch.setattr(guard, "_machine", _boom)
    assert _check(repo, "git push origin main") is None
    assert _check(repo, "git status") is None


def test_non_bash_tool_allows(repo):
    assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": "x"}}) is None


def _registry(tmp_path, monkeypatch, mirror_path, track_ref="origin/candidate"):
    reg = tmp_path / "registry"
    reg.mkdir()
    (reg / "registry.local.toml").write_text(
        f'"publish.mirrors.mirror_x.path" = "{mirror_path.as_posix()}"\n'
        f'"publish.mirrors.mirror_x.track_ref" = "{track_ref}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))


def test_publish_mirror_declared_branch_push_allowed(repo, tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    _registry(tmp_path, monkeypatch, mirror)
    assert _check(repo, f"git -C {mirror.as_posix()} push origin candidate") is None


def test_publish_mirror_other_branch_still_denied(repo, tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    _registry(tmp_path, monkeypatch, mirror)
    assert _denied(_check(repo, f"git -C {mirror.as_posix()} push origin topic-x"))


def test_non_mirror_repo_candidate_push_denied(repo, tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    _registry(tmp_path, monkeypatch, mirror)
    assert _denied(_check(repo, "git push origin candidate"))


def _msys(path, lower=False):
    s = path.as_posix()
    drive = s[0].lower() if lower else s[0].upper()
    return f"/{drive}{s[2:]}"


@pytest.mark.skipif(os.name != "nt", reason="MSYS drive paths translate on Windows only")
@pytest.mark.parametrize("lower", [False, True])
def test_publish_mirror_push_via_msys_dash_c(repo, tmp_path, monkeypatch, lower):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    _registry(tmp_path, monkeypatch, mirror)
    assert _check(repo, f"git -C {_msys(mirror, lower)} push origin candidate") is None
    assert _denied(_check(repo, f"git -C {_msys(mirror, lower)} push origin topic-x"))


@pytest.mark.skipif(os.name != "nt", reason="MSYS drive paths translate on Windows only")
def test_publish_mirror_push_from_msys_cwd(repo, tmp_path, monkeypatch):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    _registry(tmp_path, monkeypatch, mirror)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin candidate"},
        "cwd": _msys(mirror),
    }
    assert guard.check(payload) is None


CLOUD = {"CLAUDE_CODE_REMOTE": "true"}


def test_cloud_session_may_push_its_harness_claude_branch(repo):
    assert not _denied(_check(repo, "git push -u origin claude/fix-abc123", env=CLOUD))


def test_cloud_session_still_denies_a_non_claude_topic_branch(repo):
    assert _denied(_check(repo, "git push origin fix/foo", env=CLOUD))


def test_claude_branch_denied_outside_cloud(repo):
    assert _denied(_check(repo, "git push origin claude/fix-abc123", env={}))


def test_deny_says_the_whole_command_did_not_run(repo):
    out = _check(repo, "echo hi > f.txt && git push origin fix/foo")
    assert "whole command did not run" in out["hookSpecificOutput"]["permissionDecisionReason"]


SHA_A = "a" * 40
SHA_B = "b" * 40


def _hold(repo, text):
    cfg = repo / ".git" / "config"
    cfg.write_text(text, encoding="utf-8")


def _branch_hold(repo, allow=None):
    extra = f"\tcoordinatorPushHoldAllow = {allow}\n" if allow else ""
    _hold(repo, f'[branch "{DAY}"]\n\tcoordinatorPushHold = pr freeze\n{extra}')


def test_held_branch_push_refused(repo):
    _branch_hold(repo)
    for cmd in ("git push", "git push origin HEAD", f"git push origin {DAY}", f"git push origin x:{DAY}"):
        out = _check(repo, cmd)
        assert _denied(out), cmd
    assert "pr freeze" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "push-hold clear" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_override_env_does_not_clear_hold(repo):
    _branch_hold(repo)
    assert _denied(_check(repo, f"{KEY}=because git push origin HEAD"))


def test_allowed_sha_refspec_admitted(repo):
    _branch_hold(repo, SHA_A)
    assert not _denied(_check(repo, f"git push origin {SHA_A}:refs/heads/{DAY}"))
    assert not _denied(_check(repo, f"git push origin {SHA_A[:9]}:{DAY}"))


def test_other_sha_or_bare_push_refused_with_allow(repo):
    _branch_hold(repo, SHA_A)
    assert _denied(_check(repo, f"git push origin {SHA_B}:refs/heads/{DAY}"))
    assert _denied(_check(repo, "git push origin HEAD"))


def test_unheld_branch_unaffected(repo):
    _hold(repo, '[branch "work/machine-a/other"]\n\tcoordinatorPushHold = n\n')
    assert not _denied(_check(repo, "git push origin HEAD"))
    assert not _denied(_check(repo, "git push"))


def test_repo_hold_blocks_any_branch(repo):
    _hold(repo, "[coordinator]\n\tpushHold = freeze\n")
    assert _denied(_check(repo, "git push origin main"))
    assert _denied(_check(repo, f"git push origin HEAD"))


def test_dash_c_resolves_hold_repo(repo, tmp_path_factory):
    _branch_hold(repo)
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    payload = {"tool_name": "Bash", "tool_input": {"command": f'git -C "{repo}" push origin HEAD'},
               "cwd": str(elsewhere), "env": {}}
    assert _denied(guard.check(payload))


@pytest.mark.parametrize("cmd", [f"git push origin :{DAY}", f"git push --delete origin {DAY}", f"git push origin -d {DAY}"])
def test_delete_of_held_branch_refused_even_with_allow(repo, cmd):
    _branch_hold(repo, SHA_A)
    assert _denied(_check(repo, cmd))

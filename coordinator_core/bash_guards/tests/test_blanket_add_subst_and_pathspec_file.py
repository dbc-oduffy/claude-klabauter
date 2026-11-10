"""check_blanket_git_add: a ``$(...)`` file list is checked by what it names,
and ``--pathspec-from-file`` paths are checked like named pathspecs."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from coordinator_core.bash_guards import _dc_git_probe, _dc_git_shared
from coordinator_core.bash_guards import dispatch_checks as guard
from coordinator_core.bash_guards._dc_blanket_add import _mask_command_substitutions
from coordinator_core.guard_messages.testing import envelope_of


def _check(cmd):
    return envelope_of(guard.check_blanket_git_add(cmd, session_id="sess1", hook_payload={}))


def _decision(result):
    return result["hookSpecificOutput"]["permissionDecision"]


@pytest.fixture
def repo(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "dir").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda r: True)
    calls = []

    def _fake_run_git(args, cwd=None, timeout=2.0, extra_env=None):
        calls.append(list(args))
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return 0, str(root) + "\n"
        if args[:2] == ["add", "--dry-run"]:
            return 0, "add 'dir/peer.py'\n"
        raise AssertionError("unexpected git invocation: %r" % (args,))

    monkeypatch.setattr(_dc_git_probe, "_run_git", _fake_run_git)

    def _no_spawn(*a, **k):
        raise AssertionError("guard path spawned a subprocess")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    monkeypatch.setattr(subprocess, "Popen", _no_spawn)

    fake = SimpleNamespace(claims={}, degraded=False)

    class _Touch:
        sink_path = staticmethod(lambda d: d)
        project_live_claims = staticmethod(lambda *s, cwd=None: fake)

    monkeypatch.setitem(
        __import__("sys").modules, "coordinator_core.session.touch_record", _Touch
    )
    monkeypatch.setattr(
        "coordinator_core.bash_guards._dc_blanket_add._bt_no_peer_can_share_the_index",
        lambda root, sid: False,
    )
    return SimpleNamespace(root=root, calls=calls)


def test_subst_file_list_is_not_swept_as_directory(repo):
    cmd = "git add -- $(git status --short dir | awk '{print $2}')"
    result = _check(cmd)
    assert result is None
    assert not any(c[:2] == ["add", "--dry-run"] for c in repo.calls)


def test_directory_named_directly_still_denied(repo):
    result = _check("git add -- dir")
    assert result is not None and _decision(result) == "deny"


def test_blanket_add_inside_substitution_still_denied(repo):
    result = _check("echo $(git add -A)")
    assert result is not None and _decision(result) == "deny"


def test_root_pathspec_after_substitution_still_denied(repo):
    result = _check("git add -- $(git status --short dir) .")
    assert result is not None and _decision(result) == "deny"


def test_mask_helper_nested_and_quoted():
    masked, inners = _mask_command_substitutions("a $(b $(c)) '$(d)' `e` $(open")
    assert masked == "a __SUBST__ '$(d)' __SUBST__ $(open"
    assert "b __SUBST__" in inners and "c" in inners and "e" in inners


def test_pathspec_file_listing_root_is_denied(repo):
    (repo.root / "list.txt").write_text(".\n", encoding="utf-8")
    result = _check("git add --pathspec-from-file=list.txt")
    assert result is not None and _decision(result) == "deny"


def test_pathspec_file_separate_arg_form(repo):
    (repo.root / "list.txt").write_text("a.py\n.\n", encoding="utf-8")
    result = _check("git add --pathspec-from-file list.txt")
    assert result is not None and _decision(result) == "deny"


def test_pathspec_file_listing_foreign_directory_is_denied(repo):
    (repo.root / "list.txt").write_text("dir\n", encoding="utf-8")
    result = _check("git add --pathspec-from-file=list.txt")
    assert result is not None and _decision(result) == "deny"
    assert "dir/peer.py" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_pathspec_file_listing_own_files_passes(repo):
    (repo.root / "list.txt").write_text("a.py\nb.py\r\n", encoding="utf-8")
    assert _check("git add --pathspec-from-file=list.txt") is None


def test_pathspec_file_nul_separated(repo):
    (repo.root / "list.bin").write_bytes(b"a.py\0.\0")
    result = _check("git add --pathspec-from-file=list.bin --pathspec-file-nul")
    assert result is not None and _decision(result) == "deny"


def test_pathspec_file_resolves_against_dash_c_dir(repo):
    (repo.root / "dir" / "list.txt").write_text(".\n", encoding="utf-8")
    result = _check("git -C dir add --pathspec-from-file=list.txt")
    assert result is not None and _decision(result) == "deny"


def test_missing_pathspec_file_passes(repo):
    assert _check("git add --pathspec-from-file=nope.txt") is None


def test_pathspec_file_stdin_is_advisory_not_deny(repo):
    result = _check("git add --pathspec-from-file=-")
    assert result is not None
    assert _decision(result) != "deny"
    ctx = str(result["hookSpecificOutput"].get("additionalContext", ""))
    assert "stdin" in ctx and "--pathspec-from-file=-" in ctx


def test_pathspec_file_adds_no_git_spawn(repo):
    (repo.root / "list.txt").write_text("a.py\n", encoding="utf-8")
    assert _check("git add --pathspec-from-file=list.txt") is None
    assert repo.calls == [["rev-parse", "--show-toplevel"]]

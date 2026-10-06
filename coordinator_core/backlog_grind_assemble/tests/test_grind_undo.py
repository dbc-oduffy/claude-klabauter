"""Tests for `grind-row undo` (grind_undo.cmd_undo) on real tmp_path git repos."""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows, grind_undo
from coordinator_core.contract.grind_vocab import encode_undo_paths
from coordinator_core.session import scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "undo-test-session"


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "core.autocrlf", "false")
    (r / "mod.txt").write_bytes(b"orig\r\nbytes\x00\xff\n")
    (r / "gone.txt").write_text("gone\n")
    (r / "tool.sh").write_text("#!/bin/sh\n")
    os.chmod(r / "tool.sh", 0o755)
    (r / "other.txt").write_text("other\n")
    (r / "d").mkdir()
    (r / "d" / "inner.txt").write_text("x\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(r)
    return r


def _claim(repo, *paths):
    for p in paths:
        scope.touch(SID, p, cwd=str(repo), root=str(repo))


def _undo(repo, paths):
    return grind_rows.main(["undo", "--paths-urlenc", encode_undo_paths(paths), "--repo-root", str(repo)])


def _tree_bytes(repo):
    out = {}
    for p in sorted(repo.rglob("*")):
        if ".git" in p.relative_to(repo).parts or not p.is_file():
            continue
        out[p.relative_to(repo).as_posix()] = (p.read_bytes(), p.stat().st_mode)
    return out


def _count_spawns(monkeypatch):
    calls = []
    real = subprocess.Popen

    def spy(*a, **k):
        calls.append(a[0] if a else k.get("args"))
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return calls


def test_undo_verb_is_registered(repo, capsys):
    assert "undo" in grind_rows._VERBS
    rc = grind_rows.main(["undo"])
    assert rc == 2
    assert "grind-row undo --paths-urlenc" in capsys.readouterr().err


def test_restores_modified_deleted_and_removes_created(repo, capsys, monkeypatch):
    (repo / "mod.txt").write_bytes(b"changed")
    (repo / "gone.txt").unlink()
    (repo / "new.txt").write_text("created\n")
    os.chmod(repo / "tool.sh", 0o644)
    paths = ["mod.txt", "gone.txt", "new.txt", "tool.sh"]
    _claim(repo, *paths)
    calls = _count_spawns(monkeypatch)
    assert _undo(repo, paths) == 0
    assert capsys.readouterr().out.strip() == (
        '{"removed": ["new.txt"], "restored": ["gone.txt", "mod.txt", "tool.sh"]}'
    )
    assert (repo / "mod.txt").read_bytes() == b"orig\r\nbytes\x00\xff\n"
    assert (repo / "gone.txt").read_text() == "gone\n"
    assert not (repo / "new.txt").exists()
    assert os.stat(repo / "tool.sh").st_mode & 0o111
    assert len(calls) == 2
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout
    assert status.strip() == ""


def test_tracked_file_misfiled_as_created_is_restored(repo, capsys):
    (repo / "other.txt").write_text("edited\n")
    _claim(repo, "other.txt")
    assert _undo(repo, ["other.txt"]) == 0
    assert (repo / "other.txt").read_text() == "other\n"


def test_single_class_is_one_spawn(repo, monkeypatch):
    (repo / "new.txt").write_text("c\n")
    _claim(repo, "new.txt")
    calls = _count_spawns(monkeypatch)
    assert _undo(repo, ["new.txt"]) == 0
    assert len(calls) == 1


def test_already_absent_created_path_is_not_an_error(repo, capsys):
    _claim(repo, "never.txt")
    assert _undo(repo, ["never.txt"]) == 0
    assert capsys.readouterr().out.strip() == '{"removed": [], "restored": []}'


def test_created_symlink_is_removed_and_its_target_kept(repo, capsys):
    try:
        os.symlink("other.txt", repo / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    _claim(repo, "link.txt")
    assert _undo(repo, ["link.txt"]) == 0
    assert capsys.readouterr().out.strip() == '{"removed": ["link.txt"], "restored": []}'
    assert not (repo / "link.txt").is_symlink()
    assert (repo / "other.txt").read_text() == "other\n"


def test_git_spawn_failure_is_a_refusal(repo, monkeypatch):
    (repo / "mod.txt").write_bytes(b"changed")
    _claim(repo, "mod.txt")

    def boom(*a, **k):
        raise OSError("git cannot start")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert _undo(repo, ["mod.txt"]) == 1
    assert (repo / "mod.txt").read_bytes() == b"changed"


def test_staged_created_file_keeps_its_index_entry(repo):
    (repo / "new.txt").write_text("c\n")
    _git(repo, "add", "new.txt")
    _claim(repo, "new.txt")
    assert _undo(repo, ["new.txt"]) == 0
    ls = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "new.txt"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout
    assert ls.strip() == "new.txt"


def _refused(repo, paths, monkeypatch, expect_rc=1, spawns=0):
    before = _tree_bytes(repo)
    calls = _count_spawns(monkeypatch)
    assert _undo(repo, paths) == expect_rc
    assert _tree_bytes(repo) == before
    assert len(calls) == spawns


def test_undeclared_path_refused(repo, monkeypatch, capsys):
    (repo / "mod.txt").write_bytes(b"changed")
    (repo / "other.txt").write_text("edited\n")
    _claim(repo, "mod.txt")
    _refused(repo, ["mod.txt", "other.txt"], monkeypatch)
    assert "other.txt" in capsys.readouterr().err


def test_peer_held_path_refused(repo, monkeypatch):
    (repo / "mod.txt").write_bytes(b"changed")
    _claim(repo, "mod.txt")
    monkeypatch.setattr(scope, "contested_by_live_peers", lambda *a, **k: {"mod.txt": ["peer"]})
    _refused(repo, ["mod.txt"], monkeypatch)


def test_unknown_contest_refused(repo, monkeypatch):
    (repo / "mod.txt").write_bytes(b"changed")
    _claim(repo, "mod.txt")
    monkeypatch.setattr(scope, "contested_by_live_peers", lambda *a, **k: None)
    _refused(repo, ["mod.txt"], monkeypatch)


def test_unresolvable_session_refused(repo, monkeypatch):
    (repo / "mod.txt").write_bytes(b"changed")
    _claim(repo, "mod.txt")
    monkeypatch.delenv("COORDINATOR_SESSION_ID")
    _refused(repo, ["mod.txt"], monkeypatch)


def test_directory_refused(repo, monkeypatch):
    _claim(repo, "d")
    _refused(repo, ["d"], monkeypatch)


def test_tree_at_head_refused(repo, monkeypatch):
    _claim(repo, "d")
    (repo / "d").rename(repo / "d_moved")
    (repo / "d").write_text("now a file\n")
    _refused(repo, ["d"], monkeypatch, spawns=1)


def test_submodule_commit_at_head_refused(repo, monkeypatch):
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    ).stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},sub")
    _git(repo, "commit", "-q", "-m", "gitlink")
    (repo / "sub").write_text("file where gitlink was\n")
    _claim(repo, "sub")
    _refused(repo, ["sub"], monkeypatch, spawns=1)


@pytest.mark.parametrize("bad", ["../x", "sub/../../x"])
def test_escaping_path_is_usage_error(repo, monkeypatch, bad):
    _refused(repo, [bad], monkeypatch, expect_rc=2)


def test_absolute_path_elsewhere_is_usage_error(repo, tmp_path, monkeypatch):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    _refused(repo, [str(outside)], monkeypatch, expect_rc=2)
    assert outside.read_text() == "keep\n"


def test_malformed_token_is_usage_error(repo):
    rc = grind_rows.main(["undo", "--paths-urlenc", "%7Bnot-json", "--repo-root", str(repo)])
    assert rc == 2

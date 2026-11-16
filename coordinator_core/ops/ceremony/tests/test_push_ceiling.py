"""resolve_push_ceiling: default, LFS floor, explicit key, clamps, zero spawns."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import push_ceiling as pc
from coordinator_core.ops.ceremony.push_ceiling import (
    LFS_PUSH_CEILING_FLOOR_SECS,
    PUSH_CEILING_MAX_SECS,
    gitattributes_declares_lfs_filter,
    resolve_push_ceiling,
)
from pathlib import Path

DEFAULT = 16.0
LFS_REQUIRED = '[filter "lfs"]\n\trequired = true\n'


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    pc._GLOBAL_MEMO.clear()
    real_read = pc._read
    monkeypatch.setattr(
        pc, "_read", lambda path: "" if str(path) == "/etc/gitconfig" else real_read(path)
    )
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _local(repo, text) -> None:
    (repo / ".git" / "config").write_text(text, encoding="utf-8")


def _lfs_attrs(repo) -> None:
    (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs\n", encoding="utf-8")


def test_default_when_nothing_set(env, repo) -> None:
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == DEFAULT
    assert LFS_PUSH_CEILING_FLOOR_SECS == 60.0


def test_lfs_floor_local_required(env, repo) -> None:
    _lfs_attrs(repo)
    _local(repo, LFS_REQUIRED)
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == 60.0
    assert resolve_push_ceiling(repo, default_secs=90.0) == 90.0


def test_global_required_without_gitattributes_is_default(env, repo) -> None:
    (env / ".gitconfig").write_text(LFS_REQUIRED, encoding="utf-8")
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == DEFAULT


@pytest.mark.parametrize("where", ["gitconfig", "xdg", "override"])
def test_global_candidates_with_gitattributes(env, repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where) -> None:
    _lfs_attrs(repo)
    if where == "gitconfig":
        (env / ".gitconfig").write_text(LFS_REQUIRED, encoding="utf-8")
    elif where == "xdg":
        xdg = tmp_path / "xdg"
        (xdg / "git").mkdir(parents=True)
        (xdg / "git" / "config").write_text(LFS_REQUIRED, encoding="utf-8")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    else:
        f = tmp_path / "gc"
        f.write_text(LFS_REQUIRED, encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(f))
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == 60.0


def test_local_required_false_overrides_global(env, repo) -> None:
    _lfs_attrs(repo)
    (env / ".gitconfig").write_text(LFS_REQUIRED, encoding="utf-8")
    _local(repo, '[filter "lfs"]\n\trequired = false\n')
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == DEFAULT


def test_gitattributes_without_required_is_default(env, repo) -> None:
    _lfs_attrs(repo)
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == DEFAULT


def test_explicit_key_wins_over_lfs_floor(env, repo) -> None:
    _lfs_attrs(repo)
    _local(repo, LFS_REQUIRED + "[coordinator]\n\tpushCeilingSecs = 40\n")
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == 40.0


def test_explicit_key_clamped(env, repo) -> None:
    _local(repo, "[coordinator]\n\tpushCeilingSecs = 5\n")
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == DEFAULT
    _local(repo, "[coordinator]\n\tpushCeilingSecs = 9999\n")
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == PUSH_CEILING_MAX_SECS


@pytest.mark.parametrize("bad", ["abc", "0", "-3", "nan", "inf", ""])
def test_bad_explicit_value_treated_as_absent(env, repo, bad) -> None:
    _lfs_attrs(repo)
    _local(repo, LFS_REQUIRED + f"[coordinator]\n\tpushCeilingSecs = {bad}\n")
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == 60.0


def test_inline_comment_tolerated(env, repo) -> None:
    _local(repo, "[coordinator]\n\tpushCeilingSecs = 40 # lfs\n")
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == 40.0


def test_repo_local_read_not_memoized(env, repo) -> None:
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == DEFAULT
    _local(repo, "[coordinator]\n\tpushCeilingSecs = 50\n")
    assert resolve_push_ceiling(repo, default_secs=DEFAULT) == 50.0


def test_missing_repo_dir_is_default(env, tmp_path: Path) -> None:
    assert resolve_push_ceiling(tmp_path / "nope", default_secs=DEFAULT) == DEFAULT


def test_gitattributes_predicate(env, repo) -> None:
    assert not gitattributes_declares_lfs_filter(repo)
    (repo / ".gitattributes").write_text("# filter=lfs\n*.txt text\n", encoding="utf-8")
    assert not gitattributes_declares_lfs_filter(repo)
    _lfs_attrs(repo)
    assert gitattributes_declares_lfs_filter(repo)


def test_zero_spawns(env, repo, monkeypatch: pytest.MonkeyPatch) -> None:
    spawns = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, *a, **k) -> None:
            spawns.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    _lfs_attrs(repo)
    _local(repo, LFS_REQUIRED + "[coordinator]\n\tpushCeilingSecs = 30\n")
    resolve_push_ceiling(repo, default_secs=DEFAULT)
    pc._GLOBAL_MEMO.clear()
    _local(repo, LFS_REQUIRED)
    resolve_push_ceiling(repo, default_secs=DEFAULT)
    assert spawns == []

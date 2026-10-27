"""Config, autocrlf and git-dir resolution inside one call_io_scope run a count independent of the landed path count."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git import call_io_scope, content_hash, git_dir


def _checkin_all(root: Path, n: int) -> None:
    with call_io_scope.call_io_scope():
        for i in range(n):
            rel = f"f{i}.txt"
            content_hash._clean_filter_may_apply(root, rel)
            content_hash._text_attribute_pinned(root, rel)
            content_hash._repo_autocrlf_true(root)


def _counted(monkeypatch):
    counts = {"sys": 0, "cfg": 0, "gitdir": 0}

    def wrap(mod, name, key):
        orig = getattr(mod, name)

        def inner(*a, **k):
            counts[key] += 1
            return orig(*a, **k)

        monkeypatch.setattr(mod, name, inner)

    wrap(content_hash, "_system_gitconfig_paths_uncached", "sys")
    wrap(content_hash, "_read_config_core_autocrlf", "cfg")
    wrap(git_dir, "_resolve_git_dir_uncached", "gitdir")
    return counts


def _repo(tmp_path: Path, n: int, autocrlf: str | None) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    if autocrlf is not None:
        (root / ".git" / "config").write_text(f"[core]\n\tautocrlf = {autocrlf}\n")
    for i in range(n):
        (root / f"f{i}.txt").write_bytes(b"a\r\nb\r\n")
    return root


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(content_hash.Path, "home", staticmethod(lambda: tmp_path / "nohome"))


def test_resolution_counts_do_not_grow_with_path_count(tmp_path, monkeypatch):
    counts = _counted(monkeypatch)
    small = _repo(tmp_path / "a", 10, "true")
    _checkin_all(small, 10)
    at_10 = dict(counts)
    for k in counts:
        counts[k] = 0
    large = _repo(tmp_path / "b", 2000, "true")
    _checkin_all(large, 2000)
    assert counts == at_10
    assert counts["cfg"] <= 3 and counts["gitdir"] <= 2


@pytest.mark.parametrize(
    "value,expected", [("true", True), ("false", False), (None, False)]
)
def test_autocrlf_answer_unchanged(tmp_path, value, expected):
    root = _repo(tmp_path, 1, value)
    with call_io_scope.call_io_scope():
        assert content_hash._repo_autocrlf_true(root) is expected
    assert content_hash._repo_autocrlf_true(root) is expected


def test_outside_a_scope_nothing_is_cached(tmp_path, monkeypatch):
    counts = _counted(monkeypatch)
    root = _repo(tmp_path, 1, "true")
    content_hash._repo_autocrlf_true(root)
    content_hash._repo_autocrlf_true(root)
    assert counts["cfg"] == 4

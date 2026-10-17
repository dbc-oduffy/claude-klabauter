"""gap_reason names the gap-register row for each repo-wide or request-shaped gate and
returns None for a plain repo; it spawns nothing. Fixtures are hand-built gitdirs under
tmp_path, so no test here runs git."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import scoped_worktree_status as sws

_HEADER_V2 = b"DIRC" + (2).to_bytes(4, "big") + (0).to_bytes(4, "big")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    for name in sws._GATED_ENV:
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "index").write_bytes(_HEADER_V2)
    (root / "a").mkdir()
    return root


def _gap(root, literals=("a/f.txt",), roots=(), specs=()):
    return sws.gap_reason(root, list(literals), list(roots), list(specs))


def test_plain_repo_has_no_gap(repo):
    assert _gap(repo, roots=["a"], specs=[":(glob)a/*", ":(glob)*", ":(literal)a/"]) is None


def test_scoped_worktree_status_is_none_until_composed(repo):
    assert sws.scoped_worktree_status(repo, ["a/f.txt"], [], []) is None


def test_gap_reason_spawns_nothing(repo, monkeypatch):
    calls = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **k):
            calls.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    assert _gap(repo, roots=["a"]) is None
    (repo / ".gitmodules").write_text("")
    assert _gap(repo) is not None
    assert calls == []


@pytest.mark.parametrize(
    "name", ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"]
)
def test_row16_env(repo, monkeypatch, name):
    monkeypatch.setenv(name, "x")
    assert "row 16" in _gap(repo)


def test_row16_worktree_config_extension(repo):
    (repo / ".git" / "config").write_text("[extensions]\n\tworktreeConfig = true\n")
    assert "row 16" in _gap(repo)


def test_row16_linked_worktree_is_handled_not_gated(repo, tmp_path):
    linked = tmp_path / "linked"
    private = repo / ".git" / "worktrees" / "linked"
    private.mkdir(parents=True)
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {private}\n")
    (private / "commondir").write_text("../..\n")
    (private / "index").write_bytes(_HEADER_V2)
    assert _gap(linked, literals=["f.txt"]) is None


def test_row17_non_utf8(repo):
    assert "row 17" in _gap(repo, literals=["a/\udcff.txt"])


@pytest.mark.parametrize(
    "specs",
    [[":/"], [":(glob)a/**"], [":(glob)a*/*"], [":(top)a"], [":(glob)a/f.txt"], ["a"]],
)
def test_row14_unsupported_scope_spec(repo, specs):
    assert "row 14" in _gap(repo, specs=specs)


def test_row14_escaped_glob_dir_is_a_literal_dir(repo):
    (repo / "a[1]").mkdir()
    assert _gap(repo, specs=[":(glob)a\\[1\\]/*"]) is None


@pytest.mark.parametrize("literal", ["", "../x", "/abs", "a//b", "./a", "a\\b"])
def test_row14_noncanonical_literal(repo, literal):
    assert "row 14" in _gap(repo, literals=[literal])


def test_row8_gitmodules(repo):
    (repo / ".gitmodules").write_text("[submodule \"s\"]\n")
    assert "row 8" in _gap(repo)


def test_row8_nested_git_under_root(repo):
    (repo / "a" / ".git").mkdir()
    assert "row 8" in _gap(repo, roots=["a"])


def test_row8_nested_git_beside_literal(repo):
    (repo / "a" / ".git").write_text("gitdir: ../x\n")
    assert "row 8" in _gap(repo, literals=["a/f.txt"])


@pytest.mark.parametrize("version", [1, 4, 5])
def test_row11_index_version(repo, version):
    (repo / ".git" / "index").write_bytes(
        b"DIRC" + version.to_bytes(4, "big") + (0).to_bytes(4, "big")
    )
    assert "row 11" in _gap(repo)


def test_row11_index_v3_is_allowed(repo):
    (repo / ".git" / "index").write_bytes(b"DIRC" + (3).to_bytes(4, "big") + (0).to_bytes(4, "big"))
    assert _gap(repo) is None


def test_row11_bad_header(repo):
    (repo / ".git" / "index").write_bytes(b"junk")
    assert "row 11" in _gap(repo)


def test_row11_split_index_file(repo):
    (repo / ".git" / ("sharedindex." + "0" * 40)).write_bytes(b"")
    assert "row 11" in _gap(repo)


def test_row11_split_and_sparse_index_config(repo):
    (repo / ".git" / "config").write_text("[core]\n\tsplitIndex = true\n")
    assert "row 11" in _gap(repo)
    (repo / ".git" / "config").write_text("[index]\n\tsparse = true\n")
    assert "row 11" in _gap(repo)


def test_row7_sparse_checkout(repo):
    (repo / ".git" / "config").write_text("[core]\n\tsparseCheckout = true\n")
    assert "row 7" in _gap(repo)
    (repo / ".git" / "config").write_text("")
    (repo / ".git" / "info").mkdir()
    (repo / ".git" / "info" / "sparse-checkout").write_text("/*\n")
    assert "row 7" in _gap(repo)


def test_row13_filemode_true_gates_on_windows_only(repo, monkeypatch):
    (repo / ".git" / "config").write_text("[core]\n\tfilemode = true\n")
    monkeypatch.setattr(sws, "_IS_WINDOWS", True)
    assert "row 13" in _gap(repo)
    monkeypatch.setattr(sws, "_IS_WINDOWS", False)
    assert _gap(repo) is None


def test_row3_excludesfile_unparseable(repo):
    (repo / ".git" / "config").write_text("[core]\n\texcludesFile = relative/ignore\n")
    assert "row 3" in _gap(repo)


def test_row3_excludesfile_tilde_and_absolute_ok(repo, tmp_path):
    (repo / ".git" / "config").write_text("[core]\n\texcludesFile = ~/.gitignore_global\n")
    assert _gap(repo) is None
    (repo / ".git" / "config").write_text(f"[core]\n\texcludesFile = {tmp_path.as_posix()}/ig\n")
    assert _gap(repo) is None


def test_row3_global_include_gates(repo):
    (Path.home() / ".gitconfig").write_text("[includeIf \"gitdir:~/x/\"]\n\tpath = y\n")
    assert "row 3" in _gap(repo)


def test_local_config_beats_global_last_wins(repo):
    (Path.home() / ".gitconfig").write_text("[core]\n\tsplitIndex = true\n")
    (repo / ".git" / "config").write_text("[core]\n\tsplitIndex = false\n")
    assert _gap(repo) is None

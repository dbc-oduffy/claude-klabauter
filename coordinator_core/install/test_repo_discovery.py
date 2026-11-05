"""Tests for repo_discovery: origin-remote identification, zero spawns."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.install import repo_discovery as rd


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _clone(root: Path, dirname: str, url: str | None) -> Path:
    d = root / dirname
    (d / ".git").mkdir(parents=True)
    body = "[core]\n\tbare = false\n"
    if url is not None:
        body += f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    (d / ".git" / "config").write_text(body, encoding="utf-8")
    return d


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/dbc-oduffy/example-retrieval-repo.git",
        "https://github.com/dbc-oduffy/example-retrieval-repo",
        "git@github.com:dbc-oduffy/example-retrieval-repo.git",
        "ssh://git@github.com/dbc-oduffy/example-retrieval-repo.git",
        "https://github.com/dbc-oduffy/example-retrieval-repo.git/",
    ],
)
def test_remote_repo_name_forms(tmp_path, url):
    assert rd.remote_repo_name(_clone(tmp_path, "whatever", url)) == "project-rag"


def test_remote_repo_name_none_cases(tmp_path):
    assert rd.remote_repo_name(_clone(tmp_path, "noremote", None)) is None
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")
    assert rd.remote_repo_name(wt) is None


def test_discover_by_remote_not_dirname(tmp_path):
    rag = _clone(tmp_path, "rag-checkout", "https://github.com/dbc-oduffy/example-retrieval-repo.git")
    gw = _clone(tmp_path, "_general-work", "git@github.com:dbc-oduffy/general-work.git")
    _clone(tmp_path, "other", "https://github.com/x/unrelated.git")
    wanted = rd.wanted_for_keys(["repos.project_rag", "repos.general_work", "repos.missing"])
    found = rd.discover_clones([tmp_path], wanted)
    assert found == {
        "repos.project_rag": rag,
        "repos.general_work": gw,
        "coordinator.general_work_root": gw,
    }


def test_no_match_registers_nothing(tmp_path):
    _clone(tmp_path, "a", "https://github.com/x/unrelated.git")
    assert rd.discover_clones([tmp_path], {"repos.project_rag": "project_rag"}) == {}


def test_worktree_file_skipped(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: x\n", encoding="utf-8")
    assert rd.discover_clones([tmp_path], {"repos.project_rag": "project_rag"}) == {}


def test_ambiguous_registers_neither(tmp_path):
    _clone(tmp_path, "one", "https://github.com/a/example-retrieval-repo.git")
    root2 = tmp_path / "sub"
    root2.mkdir()
    _clone(root2, "two", "https://github.com/b/example-retrieval-repo.git")
    wanted = {"repos.project_rag": "project_rag"}
    found, amb = rd.find_clones([tmp_path, root2], wanted)
    assert found == {} and amb == ["repos.project_rag"]
    assert rd.discover_clones([tmp_path, root2], wanted) == {}


def test_same_clone_via_duplicate_roots_not_ambiguous(tmp_path):
    clone = _clone(tmp_path, "one", "https://github.com/a/example-retrieval-repo.git")
    found, amb = rd.find_clones([tmp_path, tmp_path], {"repos.project_rag": "project_rag"})
    assert found == {"repos.project_rag": clone} and amb == []


def test_immediate_children_only(tmp_path):
    nested = tmp_path / "deep"
    nested.mkdir()
    _clone(nested, "rag", "https://github.com/a/example-retrieval-repo.git")
    assert rd.discover_clones([tmp_path], {"repos.project_rag": "project_rag"}) == {}


def test_missing_scan_root_ok(tmp_path):
    assert rd.discover_clones([tmp_path / "nope"], {"repos.project_rag": "project_rag"}) == {}


def test_declared_repo_keys_shared_layer_only(tmp_path, monkeypatch):
    from coordinator_core import _settings_home

    (tmp_path / "registry.toml").write_text(
        '[repos]\nexample_retrieval_repo = ""\ngeneral_work = ""\n\n'
        '[regeneratability]\nexample_retrieval_repo = "x"\n\n[coordinator]\ngeneral_work_root = ""\n',
        encoding="utf-8",
    )
    (tmp_path / "registry.local.toml").write_text('[repos]\nlocal_only = "/p"\n', encoding="utf-8")
    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path)
    assert sorted(rd.declared_repo_keys()) == ["repos.general_work", "repos.project_rag"]


def test_declared_repo_keys_absent_file(tmp_path, monkeypatch):
    from coordinator_core import _settings_home

    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path)
    assert rd.declared_repo_keys() == []

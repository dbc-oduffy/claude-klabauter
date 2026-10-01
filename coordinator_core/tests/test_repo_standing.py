"""repo_standing: registered / onboarded / install-clone predicate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import engine_root, machine_profile, machine_resolver, repo_standing


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("repo_standing must not spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(machine_resolver, "merged_flat_registry", lambda: {})
    monkeypatch.setattr(machine_profile, "machine_profile", lambda: "author")
    monkeypatch.setattr(engine_root, "published_engine_mirror_path", lambda: None)


def test_registered_key_match(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.setattr(machine_resolver, "merged_flat_registry", lambda: {"repos.foo": str(repo), "repos.bar": str(tmp_path / "x")})
    assert repo_standing.repo_standing(repo).registered_key == "repos.foo"


def test_unregistered(tmp_path):
    assert repo_standing.repo_standing(tmp_path).registered_key is None


def test_unreadable_registry_degrades(tmp_path, monkeypatch):
    def bad():
        raise OSError("nope")

    monkeypatch.setattr(machine_resolver, "merged_flat_registry", bad)
    assert repo_standing.repo_standing(tmp_path).registered_key is None


@pytest.mark.parametrize("sub", [("archive",), ("state", "workstreams")])
def test_onboarded_arms(tmp_path, sub):
    assert repo_standing.repo_standing(tmp_path).onboarded is False
    tmp_path.joinpath(*sub).mkdir(parents=True)
    assert repo_standing.repo_standing(tmp_path).onboarded is True


def test_plugin_cache_path_is_install_clone(tmp_path):
    inside = repo_standing.plugin_cache_root() / "mkt" / "coord" / "1.0"
    inside.mkdir(parents=True)
    assert repo_standing.is_install_clone(inside) is True
    assert repo_standing.is_install_clone(tmp_path) is False


def test_cache_sibling_prefix_is_not_inside(tmp_path):
    sibling = Path(str(repo_standing.plugin_cache_root()) + "-other")
    sibling.mkdir(parents=True)
    assert repo_standing.is_install_clone(sibling) is False


def test_engine_clone_consumer_vs_author(tmp_path, monkeypatch):
    clone = tmp_path / "mirror"
    clone.mkdir()
    monkeypatch.setattr(engine_root, "published_engine_mirror_path", lambda: str(clone))
    assert repo_standing.is_install_clone(clone) is False
    monkeypatch.setattr(machine_profile, "machine_profile", lambda: "consumer")
    assert repo_standing.is_install_clone(clone) is True

"""Scope gate of `record_engine_provenance`: registered/onboarded repo or author box only."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import engine_provenance_counter as epc
from coordinator_core import machine_profile, repo_standing


def _standing(path, registered=None, onboarded=False):
    return repo_standing.RepoStanding(
        path=str(path), registered_key=registered, onboarded=onboarded, install_clone=False
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(epc, "resolve_git_root_cheap", lambda cwd=None: str(tmp_path))
    return tmp_path


def _call():
    epc.record_engine_provenance("c", "dispatch", "match", None, None)


def _counts(repo):
    return repo / "state" / epc._COUNTS_FILENAME


def test_unonboarded_consumer_writes_nothing(repo, monkeypatch):
    monkeypatch.setattr(machine_profile, "machine_profile", lambda: "consumer")
    monkeypatch.setattr(repo_standing, "repo_standing", lambda p: _standing(p))
    _call()
    assert not _counts(repo).exists()
    assert not (repo / "state").exists()


def test_onboarded_writes(repo, monkeypatch):
    monkeypatch.setattr(machine_profile, "machine_profile", lambda: "consumer")
    monkeypatch.setattr(repo_standing, "repo_standing", lambda p: _standing(p, onboarded=True))
    _call()
    assert _counts(repo).exists()


def test_registered_writes(repo, monkeypatch):
    monkeypatch.setattr(machine_profile, "machine_profile", lambda: "consumer")
    monkeypatch.setattr(repo_standing, "repo_standing", lambda p: _standing(p, registered="repos.x"))
    _call()
    assert _counts(repo).exists()


def test_author_writes_without_standing_lookup(repo, monkeypatch):
    monkeypatch.setattr(machine_profile, "machine_profile", lambda: "author")

    def boom(p):
        raise AssertionError("author must not need repo_standing")

    monkeypatch.setattr(repo_standing, "repo_standing", boom)
    _call()
    assert _counts(repo).exists()


def test_gate_spawns_nothing(repo, monkeypatch):
    monkeypatch.setattr(machine_profile, "machine_profile", lambda: "consumer")
    monkeypatch.setattr(repo_standing, "repo_standing", lambda p: _standing(p))

    def no_spawn(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(subprocess, "run", no_spawn)
    _call()

"""Unit tests for atlas_catchup: watermark, one git log spawn, pending repairs, ACTED facts."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.cartography import atlas_catchup as ac

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_FILE_INDEX = """\
---
last_mapped: 2026-08-06
---

# File index

## Directory → system

### cartography — atlas tooling

`coordinator_core/cartography/`
"""
_PAGE = "docs/architecture/systems/cartography.md"
_PAGE_TEXT = "---\nsystem: cartography\nfiles: 7\nlines: 10\n---\n\n# cartography\n"
_NEW = "coordinator_core/cartography/new_mod.py"


@pytest.fixture
def spawns(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = ac.run_git

    def _counting(args, *a, **k):
        calls.append(["git", *args])
        return real(args, *a, **k)

    monkeypatch.setattr(ac, "run_git", _counting)
    return calls


@pytest.fixture
def no_spawn(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(ac, "run_git", _boom)


def test_no_atlas_writes_nothing_and_spawns_nothing(tmp_path, no_spawn):
    bare = tmp_path / "bare"
    (bare / ".git").mkdir(parents=True)
    (bare / ".git/HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
    res = ac.catch_up(bare)
    assert (res.ran, res.spawns) == (False, 0)
    assert not (bare / ".coordinator-local").exists()


def test_no_atlas_settle_commit_writes_nothing(tmp_path):
    ac.settle_commit(tmp_path, [_PAGE], "a" * 40, "b" * 40)
    assert not (tmp_path / ".coordinator-local").exists()

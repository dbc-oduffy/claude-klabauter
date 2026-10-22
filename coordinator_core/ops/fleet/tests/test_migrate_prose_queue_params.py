"""Param refusals of fleet.migrate_prose_queue: each fires before any file is opened."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _prose_queue_run, migrate_prose_queue


def _good(root: Path) -> dict:
    return {"repo_root": str(root), "family": ["improvement-queue"], "dry_run": True}


def _snapshot(root: Path) -> list:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "improvement-queue.md").write_text("- 2026-01-01 | x | y | z | w\n")
    return tmp_path


@pytest.fixture
def run_calls(monkeypatch):
    calls = []

    def _spy(root, families, dry_run):
        calls.append((root, families, dry_run))
        return {"exit_code": 0}

    monkeypatch.setattr(_prose_queue_run, "run_migration", _spy)
    return calls


def _call(params, root):
    return asyncio.run(migrate_prose_queue._handler(params, root))


def _mut_missing(key):
    def mutate(p):
        del p[key]
    return mutate


@pytest.mark.parametrize(
    "mutate",
    [
        _mut_missing("repo_root"),
        _mut_missing("family"),
        _mut_missing("dry_run"),
        lambda p: p.update(family=[]),
        lambda p: p.update(family="improvement-queue"),
        lambda p: p.update(family=["nope"]),
        lambda p: p.update(family=["improvement-queue", "nope"]),
        lambda p: p.update(dry_run="false"),
        lambda p: p.update(dry_run=0),
        lambda p: p.update(candidate_ids=["a"]),
        lambda p: p.update(candidate_ids=[]),
        lambda p: p.update(repo_root=""),
    ],
)
def test_param_refusal_touches_nothing(tree, run_calls, mutate):
    params = _good(tree)
    mutate(params)
    before = _snapshot(tree)
    result = _call(params, tree)
    assert result["exit_code"] == 2
    assert result["error"]
    assert run_calls == []
    assert _snapshot(tree) == before


def test_repo_root_mismatch_refused(tree, run_calls, tmp_path_factory):
    other = tmp_path_factory.mktemp("sibling")
    before = _snapshot(tree)
    result = _call(_good(other), tree)
    assert result["exit_code"] == 1
    assert "own tree" in result["error"]
    assert run_calls == []
    assert _snapshot(tree) == before


def test_missing_engine_toplevel_refused(tree, run_calls):
    result = _call(_good(tree), None)
    assert result["exit_code"] == 1
    assert run_calls == []


def test_valid_params_delegate(tree, run_calls):
    params = _good(tree)
    params["family"] = ["bug-backlog", "debt-backlog"]
    params["dry_run"] = False
    assert _call(params, tree) == {"exit_code": 0}
    assert run_calls == [(tree.resolve(), ["bug-backlog", "debt-backlog"], False)]

"""Unit tests for run_owner.owning_runs with claim_index and warp_live stubbed."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.authoring_leaks import run_owner
from coordinator_core.session import claim_index


@pytest.fixture
def stub(monkeypatch, tmp_path):
    state = {"claims": {}, "runs": {}, "lookups": 0, "run_calls": []}

    def lookup(paths, sessions_dir=None, cwd=None):
        state["lookups"] += 1
        return {p: list(state["claims"].get(p, [])) for p in paths}

    def live_run(root, sid, **kw):
        state["run_calls"].append(sid)
        return state["runs"].get(sid)

    monkeypatch.setattr(run_owner.claim_index, "lookup", lookup)
    monkeypatch.setattr(run_owner.warp_live, "live_run_for_session", live_run)
    monkeypatch.setattr(run_owner.session_core, "resolve_session_id", lambda root: "me")

    def no_spawn(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    state["root"] = tmp_path
    return state


def test_live_other_run_is_named(stub):
    stub["claims"] = {"pkg/b.py": ["s-b"]}
    stub["runs"] = {"s-b": "run-b"}
    assert run_owner.owning_runs(stub["root"], ["pkg/b.py"]) == {"pkg/b.py": "run-b"}


def test_own_session_dead_run_and_unclaimed_map_to_nothing(stub):
    stub["claims"] = {"own.py": ["me"], "dead.py": ["s-d"]}
    stub["runs"] = {"me": "run-me"}
    out = run_owner.owning_runs(stub["root"], ["own.py", "dead.py", "free.py"])
    assert out == {}
    assert "me" not in stub["run_calls"]


def test_explicit_self_session_overrides_default(stub):
    stub["claims"] = {"a.py": ["me"]}
    stub["runs"] = {"me": "run-me"}
    assert run_owner.owning_runs(stub["root"], ["a.py"], self_session="other") == {"a.py": "run-me"}


def test_unanswerable_maps_to_nothing(stub):
    stub["claims"] = {"a.py": [claim_index.UNANSWERABLE]}
    stub["runs"] = {claim_index.UNANSWERABLE: "bogus"}
    assert run_owner.owning_runs(stub["root"], ["a.py"]) == {}


def test_claimant_memoised_across_paths(stub):
    stub["claims"] = {"a.py": ["s-b"], "c.py": ["s-b"]}
    stub["runs"] = {"s-b": "run-b"}
    out = run_owner.owning_runs(stub["root"], ["a.py", "c.py"])
    assert out == {"a.py": "run-b", "c.py": "run-b"}
    assert stub["run_calls"] == ["s-b"]
    assert stub["lookups"] == 1


def test_resolve_failure_means_no_self_exclusion(stub, monkeypatch):
    def boom(root):
        raise RuntimeError("x")

    monkeypatch.setattr(run_owner.session_core, "resolve_session_id", boom)
    stub["claims"] = {"a.py": ["s-b"]}
    stub["runs"] = {"s-b": "run-b"}
    assert run_owner.owning_runs(stub["root"], ["a.py"]) == {"a.py": "run-b"}


def test_lookup_failure_returns_empty(stub, monkeypatch):
    def boom(*a, **k):
        raise OSError("x")

    monkeypatch.setattr(run_owner.claim_index, "lookup", boom)
    assert run_owner.owning_runs(stub["root"], ["a.py"]) == {}


def test_empty_paths_skip_lookup(stub):
    assert run_owner.owning_runs(stub["root"], []) == {}
    assert stub["lookups"] == 0

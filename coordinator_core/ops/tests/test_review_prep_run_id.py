"""review.prep run_id: the run's declared paths resolve from its record and match an explicit list."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE, record_dir
from coordinator_core.ops.tests.test_review_prep import _base, _spawn_trap, _write, repo  # noqa: F401

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-x"


def _stage(repo: Path, declared, footprint=None, run_id: str = RUN) -> None:
    d = record_dir(repo, run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"review_declared_paths": declared}), encoding="utf-8")
    if footprint is not None:
        (d / LEG_FOOTPRINT_FILE).write_text(json.dumps({"writes": footprint}), encoding="utf-8")


def _params(repo: Path, prefix: str, **kw) -> dict:
    return {
        "base": _base(repo), "slice_prefix": prefix, "plan_path": "docs/plans/p.md",
        "worktree": True, "session_id": "sess-own", **kw,
    }


def _shape(reply: dict):
    return [(s["id"].split("-", 2)[-1], s["files"]) for s in reply["slices"]], reply["product_files"]


@pytest.mark.parametrize(
    "rels",
    [["a/b/one.py"], [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]],
    ids=["single", "partition"],
)
def test_run_id_returns_the_same_slices_as_explicit_paths(repo, rels):
    _write(repo, rels, 30)
    _stage(repo, rels)
    by_id = rp._handler(_params(repo, "prep-r", run_id=RUN), repo)
    by_paths = rp._handler(_params(repo, "prep-p", paths=rels), repo)
    assert by_id["error"] is None and by_paths["error"] is None
    assert by_id["slices"]
    assert _shape(by_id) == _shape(by_paths)


def test_explicit_call_after_run_id_call_is_served_from_the_store(repo, monkeypatch):
    rels = [f"pkg{i % 8}/sub{i % 3}/mod{i}.py" for i in range(19)]
    _write(repo, rels, 30)
    _stage(repo, rels)
    first = rp._handler(_params(repo, "prep-s", run_id=RUN), repo)
    assert first["error"] is None
    _spawn_trap(monkeypatch)
    second = rp._handler(_params(repo, "prep-s", paths=rels), repo)
    assert second["error"] is None
    assert _shape(second) == _shape(first)


def test_leg_appends_footprint_writes_and_absent_leg_ignores_them(repo):
    _write(repo, ["a/one.py", "b/two.py"], 3)
    _stage(repo, ["a/one.py"], footprint=["b/two.py"])
    assert rp.declared_paths_for_run(repo, RUN) == ["a/one.py"]
    assert rp.declared_paths_for_run(repo, RUN, leg=True) == ["a/one.py", "b/two.py"]
    with_leg = rp._handler(_params(repo, "prep-l", run_id=RUN, leg=True), repo)
    without = rp._handler(_params(repo, "prep-n", run_id=RUN), repo)
    assert with_leg["product_files"] == 2
    assert without["product_files"] == 1


def _bad(repo, **kw):
    reply = rp._handler(_params(repo, "prep-bad", **kw), repo)
    assert reply["error"]
    assert reply["slices"] == []
    return reply["error"]


def test_refusals_return_error_and_no_slices(repo):
    _write(repo, ["a/one.py"], 3)
    assert "exactly one of paths or run_id" in _bad(repo, run_id=RUN, paths=["a/one.py"])
    assert "requires params.paths" in _bad(repo)
    assert _bad(repo, run_id="../x")
    assert "no readable manifest" in _bad(repo, run_id="ask-missing")
    _stage(repo, "a/one.py", run_id="ask-str")
    assert "not a list of str" in _bad(repo, run_id="ask-str")
    _stage(repo, ["a/one.py"], run_id="ask-nofoot")
    assert "leg footprint" in _bad(repo, run_id="ask-nofoot", leg=True)
    _stage(repo, [], run_id="ask-empty")
    assert "ask-empty" in _bad(repo, run_id="ask-empty")
    assert "leg" in _bad(repo, paths=["a/one.py"], leg=True)


def test_resolver_spawns_nothing(repo, monkeypatch):
    _stage(repo, ["a/one.py"])

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(rp, "run_git", boom)
    assert rp.declared_paths_for_run(repo, RUN) == ["a/one.py"]

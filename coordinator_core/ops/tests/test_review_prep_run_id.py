"""review.prep run_id: the run's declared paths resolve from its record and match an explicit list."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE, record_dir
from coordinator_core.ops.review_slice_id import run_prep_slice_prefix
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
    by_paths = rp._handler(_params(repo, run_prep_slice_prefix(RUN, _base(repo)), paths=rels), repo)
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
    second = rp._handler(_params(repo, run_prep_slice_prefix(RUN, _base(repo)), paths=rels), repo)
    assert second["error"] is None
    assert _shape(second) == _shape(first)


def test_footprint_writes_join_the_write_set_with_or_without_leg(repo):
    _write(repo, ["a/one.py", "b/two.py"], 3)
    _stage(repo, ["a/one.py"], footprint=["b/two.py"])
    assert rp.declared_paths_for_run(repo, RUN) == ["a/one.py", "b/two.py"]
    assert rp.declared_paths_for_run(repo, RUN, leg=True) == ["a/one.py", "b/two.py"]
    with_leg = rp._handler(_params(repo, "prep-l", run_id=RUN, leg=True), repo)
    without = rp._handler(_params(repo, "prep-n", run_id=RUN), repo)
    assert with_leg["error"] is None and without["error"] is None
    assert with_leg["product_files"] == without["product_files"] == 2


def test_run_id_handler_keeps_a_path_a_halted_peer_withholds(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import land_run

    _write(repo, ["a/theirs.py"], 3)
    _stage(repo, ["a/theirs.py"])
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda root, rid, plan, paths: {"a/theirs.py": "peer"})
    reply = rp._handler(_params(repo, "prep-w", run_id=RUN), repo)
    assert reply["error"] is None
    assert [f for s in reply["slices"] for f in s["files"]] == ["a/theirs.py"]
    assert reply["peer_held"] == ["a/theirs.py"] and reply["excluded"] == []


def test_credited_chunk_paths_slice_when_manifest_list_is_empty(repo):
    _write(repo, ["pkg/c1.py"], 3)
    (repo / "docs/plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs/plans/p.md").write_text(
        "# p\n\n## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  writes: [pkg/c1.py]\n"
        "  disposition: open\n```\n", encoding="utf-8")
    (repo / "state/sizings").mkdir(parents=True, exist_ok=True)
    (repo / "state/sizings/s.yaml").write_text('plan: "docs/plans/p.md"\n', encoding="utf-8")
    _stage(repo, [])
    d = record_dir(repo, RUN)
    (d / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8")
    (d / "credit.jsonl").write_text(json.dumps({"chunk": "C1", "evidence": "e"}) + "\n", encoding="utf-8")
    reply = rp._handler(_params(repo, "prep-c", run_id=RUN), repo)
    assert reply["error"] is None
    assert reply["slices"] and reply["product_files"] == 1


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

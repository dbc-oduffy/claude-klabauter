"""`open_probe`, given `live_runs`, never touches another live run's path and restores what it probed;
the `review.reverify` entry point supplies those `live_runs`.

`review.reverify`'s own entry point derives `live_runs` from `land_run.foreign_halted_withheld`, so a
foreign halted run's path is never probed (an empty `run_id` owns nothing, so every halted run is
foreign). Revert mode takes no `live_runs`: it asks `foreign_halted_withheld` directly and keeps a
path another run withholds (`peer-withheld`)."""

from __future__ import annotations

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops import review_reverify_probe as probe
from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.run_ownership import LiveRun
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, _ledger, _row, world as rr_world,
)


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "repo"
    snap = tmp_path / "share" / "x.pre-review"
    (root / "pkg").mkdir(parents=True)
    snap.mkdir(parents=True)
    (root / "pkg" / "mine.py").write_bytes(b"post-mine\n")
    (root / "docs").mkdir()
    (root / "docs" / "plan.md").write_bytes(b"sibling stamps\n")
    return root, snap


WRITES = {"pkg/mine.py": b"probe-mine\n", "docs/plan.md": b"probe-plan\n"}


def test_probe_refuses_a_path_another_live_run_withholds(world):
    root, snap = world
    live = {"other": LiveRun(withheld=frozenset({"docs/plan.md"}))}
    out = probe.open_probe(root, snap, WRITES, ["t::x"], run_id="me", live_runs=live)
    assert "docs/plan.md" in out["error"] and out["probed"] == []
    assert (root / "docs" / "plan.md").read_bytes() == b"sibling stamps\n"
    assert (root / "pkg" / "mine.py").read_bytes() == b"post-mine\n"
    assert not probe.is_open(snap)


def test_probe_refuses_a_path_another_live_run_claims(world):
    root, snap = world
    live = {"other": LiveRun(claims=frozenset({"docs"}))}
    out = probe.open_probe(root, snap, WRITES, ["t::x"], run_id="me", live_runs=live)
    assert out["error"] and not probe.is_open(snap)


def test_probe_refuses_a_halted_peer_on_another_plan(world):
    root, snap = world
    live = {"gone": LiveRun(plan="p2", claims=frozenset({"docs/plan.md"}))}
    out = probe.open_probe(root, snap, WRITES, ["t::x"], run_id="me", live_runs=live)
    assert "docs/plan.md" in out["error"] and not probe.is_open(snap)


def test_probe_still_opens_a_superseded_predecessors_claimed_paths(world):
    root, snap = world
    live = {"gone": LiveRun(superseded_by="me", claims=frozenset({"docs/plan.md"}))}
    out = probe.open_probe(root, snap, WRITES, ["t::x"], run_id="me", live_runs=live)
    assert out["error"] is None and out["probed"] == ["docs/plan.md", "pkg/mine.py"]
    assert probe.close_probe(root, snap)["error"] is None
    assert (root / "docs" / "plan.md").read_bytes() == b"sibling stamps\n"


def test_a_failed_midway_write_restores_every_probed_path(world, monkeypatch):
    root, snap = world
    real = probe._put
    calls = {"n": 0}

    def flaky(path, data):
        if path.is_relative_to(root):
            calls["n"] += 1
            if calls["n"] == 2 and data.startswith(b"probe-"):
                raise OSError("disk")
        real(path, data)

    monkeypatch.setattr(probe, "_put", flaky)
    out = probe.open_probe(root, snap, WRITES, ["t::x"])
    assert "restored" in out["error"]
    assert (root / "docs" / "plan.md").read_bytes() == b"sibling stamps\n"
    assert (root / "pkg" / "mine.py").read_bytes() == b"post-mine\n"
    assert not probe.is_open(snap)


def test_a_failed_restore_leaves_the_probe_open_naming_the_stuck_path(world, monkeypatch):
    root, snap = world
    real = probe._put
    calls = {"n": 0}

    def flaky(path, data):
        if path.is_relative_to(root):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk")
        real(path, data)

    monkeypatch.setattr(probe, "_put", flaky)
    out = probe.open_probe(root, snap, WRITES, ["t::x"])
    assert "probe left open" in out["error"] and "docs/plan.md" in out["error"]
    assert probe.is_open(snap)


def test_review_prep_drops_withheld_paths_from_the_reviewed_set(tmp_path):
    live = {"other": LiveRun(withheld=frozenset({"a/theirs.py"}))}
    reply = rp.review_prep(
        tmp_path, base="abcd1234", paths=["a/theirs.py"], slice_prefix="p",
        plan_path="docs/plans/p.md", session_id="s", run_id="me", live_runs=live,
    )
    assert reply["error"] is None and reply["product_files"] == 0 and not reply.get("slices")


def test_live_runs_from_params_parses_and_rejects_malformed():
    runs = rp.live_runs_from_params({"b": {"halted": True, "withheld": ["x"], "claims": ["y"]}})
    assert runs["b"] == LiveRun(withheld=frozenset({"x"}), claims=frozenset({"y"}))
    assert rp.live_runs_from_params({"b": "nope"}) is None
    assert rp.live_runs_from_params(None) is None


def _probe_open(root, sidecars, failing, **kw):
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=failing,
        sidecars=sidecars, probe="open", **kw,
    )


def test_review_reverify_entry_point_does_not_probe_a_foreign_halted_path(rr_world, monkeypatch):
    root, orig, _ = rr_world
    edited = "def test_p():\n    assert nope\n"
    (root / "tests/test_plain.py").write_text(edited, encoding="utf-8")
    sidecars = _ledger(monkeypatch, _row("F1", "tests/test_plain.py", "    assert True\n", "    assert nope\n"))
    failing = ["tests/test_plain.py::test_p"]
    seen = {}

    def held(_root, run_id, plan_rel, paths):
        seen.update(run_id=run_id, plan=plan_rel, paths=list(paths))
        return {p: "other-halted" for p in paths}

    monkeypatch.setattr(land_run, "foreign_halted_withheld", held)
    out = _probe_open(root, sidecars, failing, run_id="me", plan_path="docs/plans/p.md")
    assert "tests/test_plain.py" in out["error"] and out["probed"] == []
    assert seen == {"run_id": "me", "plan": "docs/plans/p.md", "paths": ["tests/test_plain.py"]}
    assert (root / "tests/test_plain.py").read_text(encoding="utf-8") == edited


@pytest.mark.parametrize("kw", [{"run_id": ""}, {}])
def test_probe_open_refuses_an_empty_run_id(rr_world, monkeypatch, kw):
    root, _orig, _ = rr_world
    edited = "def test_p():\n    assert nope\n"
    (root / "tests/test_plain.py").write_text(edited, encoding="utf-8")
    sidecars = _ledger(monkeypatch, _row("F1", "tests/test_plain.py", "    assert True\n", "    assert nope\n"))
    seen = {}

    def held(_root, run_id, plan_rel, paths):
        seen.update(run_id=run_id)
        return {p: "other-halted" for p in paths}

    monkeypatch.setattr(land_run, "foreign_halted_withheld", held)
    out = _probe_open(root, sidecars, ["tests/test_plain.py::test_p"], **kw)
    assert seen == {"run_id": None}
    assert "tests/test_plain.py" in out["error"] and out["probed"] == []
    assert (root / "tests/test_plain.py").read_text(encoding="utf-8") == edited


def test_revert_skips_a_path_another_run_withholds(rr_world, monkeypatch):
    root, orig, _ = rr_world
    edited = "def test_p():\n    assert nope\n"
    (root / "tests/test_plain.py").write_text(edited, encoding="utf-8")
    sidecars = _ledger(monkeypatch, _row("F1", "tests/test_plain.py", "    assert True\n", "    assert nope\n"))
    monkeypatch.setattr(
        land_run, "foreign_halted_withheld", lambda _r, _id, _p, paths: {p: "other-halted" for p in paths}
    )
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, run_id="me",
        failing=["tests/test_plain.py::test_p"], sidecars=sidecars,
    )
    assert out["error"] is None and out["reverted"] == []
    assert [(k["path"], k["findings"][0]["reason"]) for k in out["kept"]] == [("tests/test_plain.py", "peer-withheld")]
    assert (root / "tests/test_plain.py").read_text(encoding="utf-8") == edited


def test_review_reverify_entry_point_probes_when_no_foreign_run_holds_the_path(rr_world, monkeypatch):
    root, orig, _ = rr_world
    (root / "tests/test_plain.py").write_text("def test_p():\n    assert nope\n", encoding="utf-8")
    sidecars = _ledger(monkeypatch, _row("F1", "tests/test_plain.py", "    assert True\n", "    assert nope\n"))
    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda *_a, **_k: {})
    out = _probe_open(root, sidecars, ["tests/test_plain.py::test_p"], run_id="me")
    assert out["error"] is None and out["probed"] == ["tests/test_plain.py"]
    assert (root / "tests/test_plain.py").read_bytes() == orig["tests/test_plain.py"]

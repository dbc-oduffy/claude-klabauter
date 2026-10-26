"""_pre_existing_dirt_refusal ignores claimed paths absent from the tree and paths the run's own
same-plan checkpoint landed, and still names foreign dirt."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow, StageManifest

LEDGER = SimpleNamespace(items=(1,), all_of_pm_ask=False)
VERDICT = {"items": [{"id": "R1", "status": "met"}]}
PLAN = "docs/plans/p.md"


def _manifest(root, names):
    for n in names:
        if n != "absent.py":
            (root / n).write_text("x\n")
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="b", writes=tuple(names), wave=0,
        register_rows=("R1",),
    )
    return StageManifest(
        run_dir="r", rows=(row,), review_declared_paths=(), marker_path="m",
        write_baseline=wb.capture(root, names),
    )


def _own(monkeypatch, owned):
    monkeypatch.setattr(tc, "_plan_checkpoint_touched", lambda root, plan, paths: frozenset(owned))


def test_absent_and_own_checkpointed_paths_are_not_dirt(tmp_path, monkeypatch):
    m = _manifest(tmp_path, ["own.py", "absent.py"])
    _own(monkeypatch, {"own.py"})
    assert tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, PLAN) is None


def test_foreign_dirt_is_still_refused_beside_absent_and_own(tmp_path, monkeypatch):
    m = _manifest(tmp_path, ["own.py", "absent.py", "foreign.py"])
    _own(monkeypatch, {"own.py"})
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, PLAN)
    assert out["refused"] == "ledger-unmet"
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: foreign.py"


def test_without_checkpoint_evidence_the_path_is_dirt(tmp_path, monkeypatch):
    m = _manifest(tmp_path, ["own.py"])
    _own(monkeypatch, set())
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, PLAN)
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: own.py"


def _prior_record(tmp_path, slice_files):
    run_dir = tmp_path / "state" / "warp" / "ask-20261024T045125-99976d"
    run_dir.mkdir(parents=True)
    (run_dir / "run-outcome.json").write_text(
        json.dumps({"inline_review": {"prep": {"slice_files": slice_files}}})
    )
    return run_dir


def test_path_an_earlier_pass_recorded_as_written_is_not_dirt(tmp_path, monkeypatch):
    m = _manifest(tmp_path, ["prior.py"])
    _own(monkeypatch, set())
    run_dir = _prior_record(tmp_path, ["prior.py"])
    assert tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, PLAN, run_dir) is None


def test_foreign_dirt_beside_a_prior_pass_path_still_refuses(tmp_path, monkeypatch):
    m = _manifest(tmp_path, ["prior.py", "foreign.py"])
    _own(monkeypatch, set())
    run_dir = _prior_record(tmp_path, ["prior.py"])
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, PLAN, run_dir)
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: foreign.py"


SIZING = "state/sizings/2026-10-25-338db0-x.yaml"


def _bound_plan(root):
    (root / "state" / "sizings").mkdir(parents=True)
    (root / SIZING).write_text("id: x\n")
    plan = root / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(f"---\nsizing_object: {SIZING}\n---\nbody\n")
    return "docs/plans/p.md"


def _halted(root, name, sizing, withheld, prints=True):
    d = root / "state" / "warp" / name
    d.mkdir(parents=True)
    (d / "halted.json").write_text(json.dumps(
        {"status": "halted", "sizing_rel": sizing, "run_artifacts": {"withheld": withheld}}
    ))
    if prints:
        from coordinator_core.ops.dispatch_emit.halted_record_commit import _record_withheld_prints

        _record_withheld_prints(root, f"state/warp/{name}", withheld, [])
    return d


def test_predecessor_run_withheld_edit_of_the_same_sizing_is_not_dirt(tmp_path, monkeypatch):
    m = _manifest(tmp_path, ["pred.py"])
    _own(monkeypatch, set())
    plan = _bound_plan(tmp_path)
    _halted(tmp_path, "ask-20261025T100000-aaaaaa", SIZING, {"pred.py": "peer-held"})
    cur = tmp_path / "state" / "warp" / "ask-20261025T110000-bbbbbb"
    cur.mkdir(parents=True)
    assert tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, plan, cur) is None


def test_predecessor_withheld_without_prints_is_still_dirt(tmp_path, monkeypatch):
    m = _manifest(tmp_path, ["pred.py"])
    _own(monkeypatch, set())
    plan = _bound_plan(tmp_path)
    _halted(tmp_path, "ask-20261025T100000-aaaaaa", SIZING, {"pred.py": "peer-held"}, prints=False)
    cur = tmp_path / "state" / "warp" / "ask-20261025T110000-bbbbbb"
    cur.mkdir(parents=True)
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, plan, cur)
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: pred.py"


def test_predecessor_withheld_edited_after_the_halt_is_still_dirt(tmp_path, monkeypatch):
    _own(monkeypatch, set())
    plan = _bound_plan(tmp_path)
    (tmp_path / "pred.py").write_text("bytes at the halt\n")
    _halted(tmp_path, "ask-20261025T100000-aaaaaa", SIZING, {"pred.py": "peer-held"})
    m = _manifest(tmp_path, ["pred.py"])
    cur = tmp_path / "state" / "warp" / "ask-20261025T110000-bbbbbb"
    cur.mkdir(parents=True)
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, plan, cur)
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: pred.py"


def _head(monkeypatch, root, clean):
    from coordinator_core.git import git_state

    monkeypatch.setattr(
        git_state, "head_blobs",
        lambda repo, paths: {p: (0o100644, wb.fingerprint(root, p)) for p in paths if p in clean},
    )


def test_path_clean_at_head_and_unchanged_since_baseline_is_not_dirt(tmp_path, monkeypatch):
    m = _manifest(tmp_path, ["committed.py"])
    _own(monkeypatch, set())
    _head(monkeypatch, tmp_path, {"committed.py"})
    assert tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, PLAN) is None


def test_dirty_unchanged_foreign_path_beside_a_head_clean_path_still_refuses(tmp_path, monkeypatch):
    m = _manifest(tmp_path, ["committed.py", "foreign.py"])
    _own(monkeypatch, set())
    _head(monkeypatch, tmp_path, {"committed.py"})
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, PLAN)
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: foreign.py"


def test_other_sizings_withheld_and_foreign_dirt_still_refuse(tmp_path, monkeypatch):
    m = _manifest(tmp_path, ["pred.py", "other.py", "foreign.py"])
    _own(monkeypatch, set())
    plan = _bound_plan(tmp_path)
    _halted(tmp_path, "ask-20261025T100000-aaaaaa", SIZING, {"pred.py": "x"})
    _halted(tmp_path, "ask-20261025T100100-cccccc", "state/sizings/other.yaml", {"other.py": "x"})
    cur = tmp_path / "state" / "warp" / "ask-20261025T110000-bbbbbb"
    cur.mkdir(parents=True)
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, plan, cur)
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: foreign.py, other.py"

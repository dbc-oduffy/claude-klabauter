"""_pre_existing_dirt_refusal ignores claimed paths absent from the tree and paths the run's own
same-plan checkpoint landed, and still names foreign dirt."""

from __future__ import annotations

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

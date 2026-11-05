"""A refire credits the paths its resumed_from predecessor wrote (judged from the predecessor's stage
manifest, so a commit-halt with no withheld record and an empty slice still counts); foreign dirt
and a path the predecessor left as it found it stay pre-existing dirt."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow, StageManifest

LEDGER = SimpleNamespace(items=(1,), all_of_pm_ask=False)
VERDICT = {"items": [{"id": "R1", "status": "met"}]}
PRED = "ask-20261031T003103-ac5f01"
CUR = "warp-2026-10-31-x-ac5f01-20261031T004508-c53b82"


def _manifest(root, names):
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="b", writes=tuple(names), wave=0,
        register_rows=("R1",),
    )
    return StageManifest(
        run_dir="r", rows=(row,), review_declared_paths=(), marker_path="m",
        write_baseline=wb.capture(root, names),
    )


def _run(root, name, resumed_from=None, baseline=None):
    d = root / "state" / "warp" / name
    d.mkdir(parents=True)
    run = {"resumed_from": resumed_from} if resumed_from else {}
    (d / "run.json").write_text(json.dumps(run))
    if baseline is not None:
        (d / "manifest.json").write_text(json.dumps({"write_baseline": baseline}))
    return d


def _own(monkeypatch):
    monkeypatch.setattr(tc, "_plan_checkpoint_touched", lambda root, plan, paths: frozenset())


def _scenario(root, pred_baseline_of):
    """Predecessor baselined every path in ``pred_baseline_of`` with that fingerprint; the tree
    holds the bytes the refire then baselines. The predecessor record has no withheld, empty slice."""
    for n in ("wrote.py", "foreign.py"):
        (root / n).write_text("after\n")
    pred = _run(root, PRED, baseline=pred_baseline_of)
    (pred / "run-outcome.json").write_text(json.dumps({"inline_review": {"prep": {"slice_files": []}}}))
    return _run(root, CUR, resumed_from=PRED)


def test_path_the_halted_predecessor_wrote_is_not_dirt(tmp_path, monkeypatch):
    _own(monkeypatch)
    cur = _scenario(tmp_path, {"wrote.py": "blob-before"})
    m = _manifest(tmp_path, ["wrote.py"])
    assert tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, None, cur) is None


def test_foreign_dirt_beside_the_predecessor_write_still_refuses(tmp_path, monkeypatch):
    _own(monkeypatch)
    cur = _scenario(tmp_path, {"wrote.py": "blob-before"})
    pred_base = {"wrote.py": "blob-before", "foreign.py": wb.fingerprint(tmp_path, "foreign.py")}
    (tmp_path / "state/warp" / PRED / "manifest.json").write_text(json.dumps({"write_baseline": pred_base}))
    m = _manifest(tmp_path, ["wrote.py", "foreign.py"])
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, None, cur)
    assert out["refused"] == "ledger-unmet"
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: foreign.py"


def test_path_the_predecessor_never_baselined_is_dirt(tmp_path, monkeypatch):
    _own(monkeypatch)
    cur = _scenario(tmp_path, {"wrote.py": "blob-before"})
    m = _manifest(tmp_path, ["foreign.py"])
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, None, cur)
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: foreign.py"


def test_run_without_a_resume_link_credits_nothing(tmp_path, monkeypatch):
    _own(monkeypatch)
    _scenario(tmp_path, {"wrote.py": "blob-before"})
    cur = _run(tmp_path, "warp-unlinked-20261031T010000-aaaaaa")
    m = _manifest(tmp_path, ["wrote.py"])
    out = tc._pre_existing_dirt_refusal(tmp_path, m, VERDICT, LEDGER, None, cur)
    assert out["ledger_failures"][0]["reason"] == "pre-existing-dirt: wrote.py"


def test_predecessor_wrote_walks_the_resume_chain(tmp_path):
    for n in ("a.py",):
        (tmp_path / n).write_text("after\n")
    _run(tmp_path, "ask-20261031T000000-111111", baseline={"a.py": "blob-before"})
    _run(tmp_path, PRED, resumed_from="ask-20261031T000000-111111", baseline={"a.py": wb.fingerprint(tmp_path, "a.py")})
    cur = _run(tmp_path, CUR, resumed_from=PRED)
    assert wb.predecessor_wrote(tmp_path, cur, wb.capture(tmp_path, ["a.py"])) == frozenset({"a.py"})

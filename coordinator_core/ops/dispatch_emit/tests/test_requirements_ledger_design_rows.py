"""requirements_ledger.judge: a design-conformance row is always essential and met only on evidence."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import design_conformance as dc
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261016T000000-abc123"
REL = "state/sizings/2026-10-16-design.yaml"
BOARD = "https://claude.ai/design/p/abc123"
HIT = {"id": "settings", "board_url": BOARD, "artboards": ["A1"], "status": "designed", "paths": ["app/settings/page.tsx"]}


@pytest.fixture
def run(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    dc.mint_rows(tmp_path, REL, [HIT])
    ledger = rl.read_ledger(tmp_path, tmp_path / RECORD_DIR_ROOT / RUN)
    design = next(i for i in ledger.items if i.anchor == "design-registry:settings")
    others = [i for i in ledger.items if i is not design]
    return tmp_path, ledger, design, others


def _verdict(ledger, design, **item):
    items = [{"id": i.id, "status": "met"} for i in ledger.items if i is not design]
    items.append({"id": design.id, **item})
    return {"items": items}


def _review(root, name="review.md", reviewer="coordinator:senior-front-end", verdict="APPROVED"):
    path = root / name
    path.write_text(f"---\nagent_type: {reviewer}\nverdict: {verdict}\n---\nChecked {BOARD}\n", encoding="utf-8")
    return name


def test_met_with_senior_front_end_record_passes(run):
    root, ledger, design, _ = run
    out = rl.judge(ledger, _verdict(ledger, design, status="met", met_by=_review(root)), repo_root=root)
    assert out["pass"], out


def test_met_with_ratified_deviation_passes(run):
    root, ledger, design, _ = run
    (root / "ratify.md").write_text("PM ratified\n", encoding="utf-8")
    plan = "```yaml design-deviation\n- entry: settings\n  ratified_by: ratify.md\n```\n"
    out = rl.judge(
        ledger, _verdict(ledger, design, status="met", met_by="ratify.md"), repo_root=root, plan_text=plan
    )
    assert out["pass"], out


def test_met_with_neither_fails(run):
    root, ledger, design, _ = run
    out = rl.judge(ledger, _verdict(ledger, design, status="met"), repo_root=root)
    assert not out["pass"] and out["failures"][0]["id"] == design.id
    wrong = _review(root, "wrong.md", reviewer="coordinator:staff-eng")
    out = rl.judge(ledger, _verdict(ledger, design, status="met", met_by=wrong), repo_root=root)
    assert not out["pass"]


def test_met_without_repo_root_fails(run):
    root, ledger, design, _ = run
    out = rl.judge(ledger, _verdict(ledger, design, status="met", met_by=_review(root)))
    assert not out["pass"]


def test_not_met_marked_non_essential_still_fails(run):
    _root, ledger, design, _ = run
    out = rl.judge(ledger, _verdict(ledger, design, status="not_met", essential=False))
    assert not out["pass"] and out["failures"][0]["reason"] == "not met"


def test_post_commit_cannot_meet_a_design_row(run):
    _root, ledger, design, _ = run
    out = rl.judge(ledger, _verdict(ledger, design, status="not_met", remaining=["commit"]))
    assert not out["pass"]


def test_not_met_withheld_by_gated_chunk_passes_open(run):
    _root, ledger, design, _ = run
    out = rl.judge(ledger, _verdict(ledger, design, status="not_met", withheld_by="G1"), gated=["G1"])
    assert out["pass"] and out["open"] == [{"id": design.id, "reason": "withheld_by G1"}]


def test_render_labels_design_row_by_origin(run):
    _root, ledger, design, _ = run
    assert f"## {design.id} (design registry)" in rl.render(ledger, RUN)


def test_ledger_gate_supplies_root_and_plan_text(run):
    root, ledger, design, _ = run
    run_dir = root / RECORD_DIR_ROOT / RUN
    bad = tc._ledger_gate(root, run_dir, _verdict(ledger, design, status="met"))[0]
    assert bad is not None and bad["refused"] == "ledger-unmet"
    (root / "ratify.md").write_text("PM ratified\n", encoding="utf-8")
    plan = "```yaml design-deviation\n- entry: settings\n  ratified_by: ratify.md\n```\n"
    ok = tc._ledger_gate(
        root, run_dir, _verdict(ledger, design, status="met", met_by="ratify.md"), plan_text=plan
    )[0]
    assert ok is None

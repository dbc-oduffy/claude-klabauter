"""land_run._ledger_gate: a design row is met by the wave's senior-front-end sidecar or a revise-noted review."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import design_conformance as dc
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT


def _ledger_gate(root, run_dir, verdict, *, plan_text=None, inline_review=None):
    return land_run._ledger_gate(root, run_dir, verdict, None, (), {}, plan_text, inline_review or {}, None, None)

RUN = "ask-20261016T000000-abc123"
REL = "state/sizings/2026-10-16-design.yaml"
BOARD = "https://claude.ai/design/p/abc123"
HIT = {"id": "settings", "board_url": BOARD, "artboards": ["A1"], "status": "designed", "paths": ["app/settings/page.tsx"]}
SIDECAR = "state/reviews/wave-sfe.md"
NOTED = "state/reviews/noted-sfe.md"


@pytest.fixture
def run(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "reviews").mkdir(parents=True)
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
    items = [{"id": i.id, "status": "met"} for i in ledger.items]
    return tmp_path, tmp_path / RECORD_DIR_ROOT / RUN, {"items": items}


def test_case1_wave_sidecar_meets_the_row(run):
    root, run_dir, verdict = run
    (root / SIDECAR).write_text(f"Board: {BOARD}\n", encoding="utf-8")
    ir = {"wave_reviews": [{"agent_type": "coordinator:senior-front-end", "sidecar_path": SIDECAR, "verdict": "APPROVED"}]}
    assert _ledger_gate(root, run_dir, verdict, inline_review=ir)[0] is None


def test_case2_revise_noted_review_meets_the_row(run):
    root, run_dir, verdict = run
    (root / NOTED).write_text(
        f"---\nagent_type: coordinator:senior-front-end\nverdict: APPROVED\n---\nBoard: {BOARD}\n", encoding="utf-8"
    )
    rl.revise(root, RUN, note=f"the review is at {NOTED}.")
    assert _ledger_gate(root, run_dir, verdict, inline_review={})[0] is None


def test_case3_neither_still_refuses(run):
    root, run_dir, verdict = run
    out = _ledger_gate(root, run_dir, verdict, inline_review={"wave_reviews": ["junk"]})[0]
    assert out is not None and out["refused"] == "ledger-unmet"
    assert "met_by names no file" in str(out)

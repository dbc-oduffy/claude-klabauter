"""ask_stage names a refusal, never a ValueError from mint_xs_spine, for a sizing the XS mint must not take."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, _handler, stage

SIZING_REL = "state/sizings/2026-10-16-fixture.yaml"


def _sizing(repo, **extra):
    doc = {
        "intent": "Fix the thing",
        "exit_criterion": {"statement": "it works"},
        "estimate": {"tshirt": "S"},
        "plan": "docs/plans/2026-10-16-fixture.md",
        **extra,
    }
    doc = {k: v for k, v in doc.items() if v is not None}
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_s_arm_plan_edge_sizing_with_empty_writes_is_refused_by_name(tmp_path):
    _sizing(tmp_path)
    with pytest.raises(AskStageError, match="^XS-MINT-MISROUTED: "):
        stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=[])


def test_s_arm_plan_edge_sizing_with_writes_is_still_refused(tmp_path):
    _sizing(tmp_path)
    with pytest.raises(AskStageError, match="^XS-MINT-MISROUTED: "):
        stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])


def test_xs_sizing_with_empty_writes_is_refused_by_name(tmp_path):
    _sizing(tmp_path, plan=None, estimate={"tshirt": "XS"})
    with pytest.raises(AskStageError, match="^XS-MINT-NO-WRITES: "):
        stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=[])


def test_handler_replies_error_not_traceback_for_the_multi_ask_shape(tmp_path):
    _sizing(tmp_path)
    (tmp_path / ".git").mkdir()
    reply = _handler({"run_id": "r1", "sizing_path": SIZING_REL, "writes": []}, repo_root=tmp_path)
    assert reply["error"].startswith("XS-MINT-MISROUTED: ")


def test_xs_sizing_with_writes_still_mints(tmp_path):
    _sizing(tmp_path, plan=None, estimate={"tshirt": "XS"})
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert [r.id for r in m.rows] == ["X1"]

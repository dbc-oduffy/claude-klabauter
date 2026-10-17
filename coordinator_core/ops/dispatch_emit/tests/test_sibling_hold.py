"""sibling_hold: predecessor_closed evidence arms and the supersession mark's refusals."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import sibling_hold as sh

RUN = "ask-20261016T152609-443905"
PLAN = "docs/plans/2026-10-16-p.md"
SIZING = "state/sizings/2026-10-16-s.yaml"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _halt(root, **fields):
    _write(root, f"state/warp/{RUN}/halted.json", json.dumps({"status": "halted", **fields}))


def _plan(root, status, rel=PLAN):
    _write(root, rel, f"---\ntitle: p\nstatus: {status}\n---\n\nbody\n")


def test_closed_by_status_mark(tmp_path):
    _write(tmp_path, c.status_rel(RUN), json.dumps({"status": "halted", "closed_by": PLAN}))
    assert sh.predecessor_closed(tmp_path, RUN) == PLAN


def test_halt_record_plan_implemented(tmp_path):
    _halt(tmp_path, plan=PLAN, sizing_rel=SIZING)
    _plan(tmp_path, "implemented")
    assert sh.predecessor_closed(tmp_path, RUN) == PLAN


def test_sizing_plan_edge_implemented(tmp_path):
    _halt(tmp_path, sizing_rel=SIZING)
    _write(tmp_path, SIZING, f"plan: {PLAN}\nintent: x\n")
    _plan(tmp_path, "implemented")
    assert sh.predecessor_closed(tmp_path, RUN) == PLAN


def test_archived_plan_resolves_on_live_miss(tmp_path):
    _halt(tmp_path, plan=PLAN)
    _plan(tmp_path, "implemented", rel="archive/specs/2026-10/2026-10-16-p.md")
    assert sh.predecessor_closed(tmp_path, RUN) == "archive/specs/2026-10/2026-10-16-p.md"


def test_not_closed_for_unimplemented_plan_or_no_record(tmp_path):
    assert sh.predecessor_closed(tmp_path, RUN) is None
    _halt(tmp_path, plan=PLAN, sizing_rel=SIZING)
    _plan(tmp_path, "reviewed")
    assert sh.predecessor_closed(tmp_path, RUN) is None


def test_unsafe_run_id_is_not_closed(tmp_path):
    assert sh.predecessor_closed(tmp_path, "../x") is None


def test_mark_superseded_writes_once_and_is_idempotent(tmp_path):
    first = sh.mark_superseded(tmp_path, RUN, "ask-new")
    assert first == {"run_id": RUN, "superseded_by": "ask-new", "written": True}
    assert c.read_superseded_by(tmp_path, RUN) == "ask-new"
    assert sh.mark_superseded(tmp_path, RUN, "ask-new")["written"] is False


@pytest.mark.parametrize("state", ["landed", "running"])
def test_refuses_landed_or_running(tmp_path, state):
    _write(tmp_path, c.status_rel(RUN), json.dumps({"status": state}))
    with pytest.raises(sh.SupersedeRefused, match=state):
        sh.mark_superseded(tmp_path, RUN, "ask-new")
    assert c.read_superseded_by(tmp_path, RUN) is None


def test_refuses_different_by_self_and_unsafe_ids(tmp_path):
    sh.mark_superseded(tmp_path, RUN, "ask-new")
    assert "already superseded by ask-new" in sh.supersede_refusal(tmp_path, RUN, "ask-other")
    assert sh.supersede_refusal(tmp_path, "r", "r")
    assert sh.supersede_refusal(tmp_path, "../x", "n")
    assert sh.supersede_refusal(tmp_path, RUN, "a/b")
    assert c.read_superseded_by(tmp_path, RUN) == "ask-new"

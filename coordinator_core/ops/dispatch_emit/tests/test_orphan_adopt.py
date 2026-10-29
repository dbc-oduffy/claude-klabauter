"""orphan_adopt: the verdict, the adopt call and the stage rewrite."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit import orphan_adopt as oa
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, adopt_run_stage_at

SIZING = "state/sizings/2026-10-28-x.yaml"
OLD = "run-a-20261028T100000-aaaaaa"
ORPHAN = "warp-2026-10-28-x-20261028T100000-aaaaaa"
LATER = "warp-2026-10-28-x-20261028T110000-bbbbbb"


def _mk(root: Path, run: str, *, session="dead", stage="staged", sizing=SIZING, marker=None):
    d = root / RECORD_DIR_ROOT / run
    d.mkdir(parents=True, exist_ok=True)
    (d / "run-stage.json").write_text(
        json.dumps({"session_id": session, "stage": stage, "started_at": "t0", "steps_planned": 1}), encoding="utf-8"
    )
    (d / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    if marker:
        (d / "halted.json").write_text(json.dumps({"status": "halted", "halted": marker}), encoding="utf-8")
    return d


@pytest.fixture
def live(monkeypatch):
    live_ids = set()
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: sid in live_ids)
    return live_ids


def test_dead_owner_is_orphaned(tmp_path, live):
    _mk(tmp_path, ORPHAN)
    v = oa.orphan_verdict(tmp_path, ORPHAN, "me")
    assert v["orphaned"] is True and v["recorded_session"] == "dead" and v["stage"] == "staged"


def test_live_owner_refused(tmp_path, live):
    live.add("dead")
    _mk(tmp_path, ORPHAN)
    assert oa.orphan_verdict(tmp_path, ORPHAN, "me")["reason"] == "owner-live"


def test_self_refused(tmp_path, live):
    _mk(tmp_path, ORPHAN, session="me")
    assert oa.orphan_verdict(tmp_path, ORPHAN, "me")["reason"] == "self"


def test_missing_run_stage(tmp_path, live):
    assert oa.orphan_verdict(tmp_path, ORPHAN, "me")["reason"] == "no-run-stage"


def test_terminal_and_halted_refused(tmp_path, live):
    _mk(tmp_path, ORPHAN, stage="landed")
    assert oa.orphan_verdict(tmp_path, ORPHAN, "me")["reason"] == "terminal-stage"
    _mk(tmp_path, LATER, sizing="other.yaml", marker="x")
    assert oa.orphan_verdict(tmp_path, LATER, "me")["reason"] == "already-halted"


def test_later_run_on_same_sizing_supersedes(tmp_path, live):
    _mk(tmp_path, ORPHAN)
    _mk(tmp_path, LATER, session="x")
    v = oa.orphan_verdict(tmp_path, ORPHAN, "me")
    assert (v["orphaned"], v["reason"], v["superseded_by"]) == (False, "superseded", LATER)


def test_later_ask_run_on_same_sizing_supersedes_other_sizing_does_not(tmp_path, live):
    _mk(tmp_path, ORPHAN)
    _mk(tmp_path, "ask-20261028T120000-cccccc", sizing="other.yaml")
    assert oa.orphan_verdict(tmp_path, ORPHAN, "me")["orphaned"] is True
    _mk(tmp_path, "ask-20261028T130000-dddddd")
    assert oa.orphan_verdict(tmp_path, ORPHAN, "me")["superseded_by"] == "ask-20261028T130000-dddddd"


def test_orphan_pending_fire_does_not_supersede(tmp_path, live):
    _mk(tmp_path, ORPHAN)
    _mk(tmp_path, LATER, marker=oa.ORPHAN_PENDING)
    assert oa.orphan_verdict(tmp_path, ORPHAN, "me")["orphaned"] is True


def test_adopt_run_stage_rewrites_session_and_keeps_rest(tmp_path):
    d = _mk(tmp_path, ORPHAN)
    adopt_run_stage_at(d, "me")
    data = json.loads((d / "run-stage.json").read_text(encoding="utf-8"))
    assert data["session_id"] == "me" and data["adopted_from"] == ["dead"]
    assert (data["started_at"], data["stage"], data["steps_planned"]) == ("t0", "staged", 1)
    adopt_run_stage_at(d, "you")
    assert json.loads((d / "run-stage.json").read_text(encoding="utf-8"))["adopted_from"] == ["dead", "me"]


def test_adopt_run_stage_refuses_absent_file(tmp_path):
    with pytest.raises(ValueError):
        adopt_run_stage_at(tmp_path, "me")


def _call(root, run=ORPHAN, **extra):
    (root / ".git").mkdir(exist_ok=True)
    return oa._handler({"run_id": run, "session_id": "me", **extra}, repo_root=root)


def _stage(root, run=ORPHAN, *, plan=True, receipt=False):
    d = root / RECORD_DIR_ROOT / run
    if plan:
        (root / "docs/plans").mkdir(parents=True, exist_ok=True)
        (root / "docs/plans/p.md").write_text("plan", encoding="utf-8")
        (d / "p.spine.md").write_text("spine", encoding="utf-8")
    if receipt:
        from coordinator_core.ops.dispatch_emit import ask_stage_receipt as r

        (d / "manifest.json").write_bytes(b"{}")
        r.write(d, "dg", {"path": "m", "bytes": 2, "sha256": hashlib.sha256(b"{}").hexdigest()})
    return d


def test_handler_adopts_staged_run_and_writes_refire_halt(tmp_path, live):
    d = _mk(tmp_path, ORPHAN)
    _stage(tmp_path, receipt=True)
    reply = _call(tmp_path)
    assert reply["ok"] is True and reply["adopted_from"] == ["dead"] and reply["resume_stage"] == "execute"
    marker = json.loads((d / "halted.json").read_text(encoding="utf-8"))
    assert marker["halted"] == "orphaned_staged" and marker["plan"] == "docs/plans/p.md"
    assert hr.is_refire_next_action(marker["next_action"])
    assert marker["next_action"]["resume"]["phase"] == "execute"
    stage = json.loads((d / "run-stage.json").read_text(encoding="utf-8"))
    assert stage["session_id"] == "me" and stage["adopted_from"] == ["dead"]


def test_handler_halt_kind_follows_evidence(tmp_path, live):
    _mk(tmp_path, ORPHAN)
    _stage(tmp_path, plan=True)
    assert _call(tmp_path, dry_run=True)["halted"] == "orphaned_planned"
    other = "warp-2026-10-28-y-20261028T100000-aaaaaa"
    _mk(tmp_path, other, sizing="state/sizings/2026-10-28-y.yaml")
    r = _call(tmp_path, other)
    assert (r["halted"], r["resume_stage"]) == ("orphaned", "plan")
    assert "phase" not in r["next_action"]["resume"]


def test_handler_stale_receipt_is_not_landed(tmp_path, live):
    d = _mk(tmp_path, ORPHAN)
    _stage(tmp_path, receipt=True, plan=False)
    (d / "manifest.json").write_bytes(b"changed")
    assert _call(tmp_path)["halted"] == "orphaned"


def test_handler_refuses_live_and_superseded_and_writes_nothing(tmp_path, live):
    live.add("dead")
    d = _mk(tmp_path, ORPHAN)
    assert _call(tmp_path) == {"ok": False, "run_id": ORPHAN, "reason": "owner-live"}
    live.clear()
    _mk(tmp_path, LATER, session="x")
    r = _call(tmp_path)
    assert (r["ok"], r["reason"], r["superseded_by"]) == (False, "superseded", LATER)
    assert not (d / "halted.json").exists()
    assert json.loads((d / "run-stage.json").read_text(encoding="utf-8"))["session_id"] == "dead"


def test_handler_dry_run_writes_nothing(tmp_path, live):
    d = _mk(tmp_path, ORPHAN)
    assert _call(tmp_path, dry_run=True)["ok"] is True
    assert not (d / "halted.json").exists()


def test_handler_under_200ms(tmp_path, live):
    import time

    _mk(tmp_path, ORPHAN)
    t = time.process_time()
    _call(tmp_path)
    assert time.process_time() - t < 0.2


def test_adopt_call_is_accepted_as_exit_call_and_names_run(tmp_path):
    call = oa.adopt_call(ORPHAN, tmp_path)
    assert ORPHAN in call and oa.OP_NAME in call
    assert hr.is_exit_next_action(hr.exit_next_action(reason="orphan", route=hr.ROUTE_ADOPT, call=call))
    assert not hr.is_exit_next_action(hr.exit_next_action(reason="r", route="nope", call=call))

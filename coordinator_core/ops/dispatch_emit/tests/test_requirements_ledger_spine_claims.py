"""workflow.revise reports register rows no spine row claims, forces an add-task `next`, and flags
requirement-shaped free-text notes that mint no row (d9a207 and 0ae085 shapes)."""

from __future__ import annotations

import json

import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "ask-20261106T000000-abc123"
REL = "state/sizings/2026-11-06-spine.yaml"
PLAN = "docs/plans/2026-11-06-spine.md"


def _plan(claimed: list[str]) -> str:
    fm = {
        "title": "p",
        "register_claims": {"sizing": REL, "rows": sorted({"R1", *claimed}), "pins": {}},
    }
    spine = [{"id": "C1", "title": "a", "writes": ["pkg/a.py"], "register_rows": claimed}]
    return (
        "---\n" + yaml.safe_dump(fm) + "---\n# p\n\n## Tasks\n\n```yaml plan-tasks\n"
        + yaml.safe_dump(spine) + "```\n"
    )


def _repo(tmp_path, claimed=("R1",)):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "plans").mkdir()
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}], "plan": PLAN,
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    (tmp_path / PLAN).write_text(_plan(list(claimed)), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


def _live(monkeypatch, value=True):
    monkeypatch.setattr(rl, "_run_driven", lambda *a, **k: value)


def test_d9a207_shape_unclaimed_revise_row_reports_and_forces_add_task(tmp_path, monkeypatch):
    _live(monkeypatch)
    root = _repo(tmp_path)
    out = rl._handler({"run_id": RUN, "verbatim": "produce the join and noun_map@1"}, repo_root=root)
    assert out["item"] == "R2"
    assert out["unclaimed_rows"] == ["R2"]
    assert "no plan row claims register row(s) R2" in out["warning"]
    assert out["next"]["kind"] == "add-task"
    payload = json.loads(out["next"]["call"].split("'", 1)[1].rsplit("'", 1)[0])
    assert payload["verb"] == "add-task" and payload["plan_path"] == PLAN
    assert payload["task"]["register_rows"] == ["R2"]


def test_spine_claiming_the_new_row_reports_nothing(tmp_path, monkeypatch):
    _live(monkeypatch)
    root = _repo(tmp_path, claimed=("R1", "R2"))
    out = rl._handler({"run_id": RUN, "verbatim": "produce the join"}, repo_root=root)
    assert out["item"] == "R2"
    assert "unclaimed_rows" not in out and "next" not in out


def test_register_claims_alone_no_longer_discharges_a_row(tmp_path, monkeypatch):
    _live(monkeypatch)
    root = _repo(tmp_path, claimed=())
    out = rl._handler({"run_id": RUN, "note": "context only"}, repo_root=root)
    assert out["unclaimed_rows"] == ["R1"]
    assert out["next"]["kind"] == "add-task"


def test_not_live_run_keeps_the_refire_leg(tmp_path, monkeypatch):
    _live(monkeypatch, False)
    root = _repo(tmp_path)
    out = rl._handler({"run_id": RUN, "verbatim": "produce the join"}, repo_root=root)
    assert out["unclaimed_rows"] == ["R2"]
    assert out["next"]["kind"] == "refire"


def test_0ae085_shape_requirement_note_mints_no_row(tmp_path, monkeypatch):
    _live(monkeypatch)
    root = _repo(tmp_path, claimed=("R1",))
    out = rl._handler({"run_id": RUN, "note": "the gate must refuse an unclaimed row"}, repo_root=root)
    assert out["ok"] is True and out["item"] is None
    assert "mints no register row" in out["note_mints_no_row"]
    assert "unclaimed_rows" not in out


def test_plain_note_has_no_mints_no_row_line(tmp_path, monkeypatch):
    _live(monkeypatch)
    root = _repo(tmp_path)
    out = rl._handler({"run_id": RUN, "note": "fyi the box rebooted"}, repo_root=root)
    assert "note_mints_no_row" not in out

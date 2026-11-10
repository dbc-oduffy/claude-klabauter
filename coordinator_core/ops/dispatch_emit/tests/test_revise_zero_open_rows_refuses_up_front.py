"""workflow.revise on a finished run whose plan omits only register rows nothing still owes offers no refire."""

from __future__ import annotations

import json

import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.warp_fire import fire_command

RUN = "ask-20261110T000000-abc123"
REL = "state/sizings/2026-11-10-revise-idle.yaml"
PLAN = "docs/plans/2026-11-10-revise-idle.md"


def _repo(tmp_path, *, omitted_status, wired=True):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    row = {
        "id": "R1", "status": omitted_status, "source_text": "t", "anchor": "relayed",
        "surface": "internal", "claimed_by": [],
    }
    if omitted_status in ("met", "waived"):
        row["ruling"] = {"source": "pm", "quote": "t", "on": "2026-11-10", "ref": "fixture"}
    if wired:
        row["wired"] = True
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "plan": PLAN,
        "requirement_register": {"rows": [row]},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    fm = {"title": "p", "register_claims": {"sizing": REL, "rows": []}}
    (tmp_path / PLAN).write_text("---\n" + yaml.safe_dump(fm) + "---\n# p\n", encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


def _revise_note(root):
    return rl._handler({"run_id": RUN, "note": "keep the reply short"}, repo_root=root)


def test_no_open_row_offers_no_refire_and_states_the_reason_once(tmp_path):
    out = _revise_note(_repo(tmp_path, omitted_status="met"))
    assert "next" not in out
    assert "re-plan" in out["note"] and "R1" in out["note"]
    assert "warning" not in out and "unclaimed_rows" in out
    assert fire_command(REL) not in json.dumps(out)
    assert json.dumps(out).count("no register row is open") == 1


def test_a_waived_row_is_not_open_either(tmp_path):
    out = _revise_note(_repo(tmp_path, omitted_status="waived", wired=False))
    assert "next" not in out and "no register row is open" in out["note"]


def test_an_open_row_keeps_the_refire_with_replans(tmp_path):
    out = _revise_note(_repo(tmp_path, omitted_status="open"))
    assert out["next"] == {"kind": "refire", "call": fire_command(REL), "replans": {"plan": PLAN, "rows": ["R1"]}}
    assert "ledger re-plan leg adds rows" in out["note"]


def test_a_met_row_not_wired_keeps_the_refire(tmp_path):
    out = _revise_note(_repo(tmp_path, omitted_status="met", wired=False))
    assert out["next"]["kind"] == "refire" and out["next"]["replans"]["rows"] == ["R1"]


def test_a_deferred_row_is_not_open_either(tmp_path):
    root = _repo(tmp_path, omitted_status="deferred", wired=False)
    out = _revise_note(root)
    assert "next" not in out and "no register row is open" in out["note"]


def test_a_halted_run_words_the_refusal_halted_and_offers_no_refire(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_run

    root = _repo(tmp_path, omitted_status="met")
    halted_run.write_halt_marker(rl.record_dir(root, RUN), halted="x", sizing_rel=REL)
    out = _revise_note(root)
    assert "next" not in out
    assert out["note"].startswith("run halted: no refire offered, no register row is open")


def test_an_unreadable_register_still_owes_work(tmp_path, monkeypatch):
    root = _repo(tmp_path, omitted_status="met")

    def unreadable(_text):
        raise OSError("unreadable")

    assert rl._rows_owe_work(root, REL, ["R1"]) is False
    monkeypatch.setattr(rl.rr, "read_register", unreadable)
    assert rl._rows_owe_work(root, REL, ["R1"]) is True

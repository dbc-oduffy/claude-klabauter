"""workflow.revise on a live run: the reply branches on whether the verify leg is staged."""

from __future__ import annotations

import json

import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

RUN = "ask-20261029T000000-aaa111"
REL = "state/sizings/2026-10-29-late-row.yaml"


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path


def _revise(root, monkeypatch):
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    return rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)


def test_before_verify_the_reply_names_the_pre_verify_leg(tmp_path, monkeypatch):
    out = _revise(_repo(tmp_path), monkeypatch)
    assert "lands_on_refire" not in out and "next" not in out
    assert "pre-verify late-row leg" in out["note"]
    assert "executor leg" in out["note"] and "no halt and no second fire" in out["note"]
    assert "never judge" not in out["note"]
    assert "next stage claims" not in json.dumps(out)


def test_after_verify_the_reply_says_it_lands_on_refire(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    (record_dir(root, RUN) / rl.VERIFY_READ_MARKER).write_text("1", encoding="utf-8")
    out = _revise(root, monkeypatch)
    assert out["ok"] is True and out["lands_on_refire"] is True
    assert out["next"]["kind"] == "refire"
    assert "never judge" not in out["note"] and "no leg of this run" in out["note"]
    assert "next stage claims" not in json.dumps(out)


def _script(root, text):
    (record_dir(root, RUN) / "resume-script.workflow.mjs").write_text(text, encoding="utf-8")


def test_served_set_reads_a_staged_but_undispatched_row_as_late(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    item = _revise(root, monkeypatch)["item"]
    every = [i.id for i in rl.read_ledger(root, record_dir(root, RUN)).items]
    others = [i for i in every if i != item]
    (record_dir(root, RUN) / "manifest.json").write_text(
        json.dumps({"ledger_item_ids": every, "rows": []}), encoding="utf-8")
    assert rl.late_rows(root, RUN) == []
    assert rl.late_rows(root, RUN, served=others) == [item]
    assert rl.late_rows(root, RUN, served=every) == []
    out = rl._late_rows_handler({"run_id": RUN, "served": others}, repo_root=root)
    assert out[rl.LATE_ROWS_FIELD] == [item]


def test_script_without_the_late_leg_refuses_the_note_at_send(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _script(root, "export default async () => {};\n")
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    out = rl._handler({"run_id": RUN, "note": "cover Windows"}, repo_root=root)
    assert out["ok"] is False and out["will_not_read"] is True
    assert {"input": "note", "reason": "run_will_not_read"} in out["refused"]
    assert "pre-verify late-row leg" not in out["note"]


def test_script_with_the_late_leg_keeps_the_promise(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _script(root, "  _lateLeg = async (_ids) => {};\n")
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    out = rl._handler({"run_id": RUN, "note": "cover Windows"}, repo_root=root)
    assert out["ok"] is True and out["refused"] == [] and "will_not_read" not in out
    assert "pre-verify late-row leg" in out["note"]


def test_no_delta_request_surface_remains():
    assert not hasattr(rl, "DELTA_REQUEST_FILE") and not hasattr(rl, "_record_delta_request")

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops import pm_directive_map as PD
from coordinator_core.session_baton import store

FIXTURE = (Path(__file__).resolve().parents[2] / "hooks" / "tests" / "fixtures"
           / "pm_turns_two_directives.jsonl")
SID = "sid-turn20"


@pytest.fixture
def session(tmp_path, monkeypatch):
    sdir = tmp_path / "coordinator-sessions" / SID
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "coordinator-sessions" / s)
    shutil.copy(FIXTURE, sdir / PT.PM_TURNS_FILENAME)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return sdir, home


def _task(home: Path, name: str, subject: str, description: str = "") -> None:
    d = home / ".claude" / "tasks" / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(
        json.dumps({"subject": subject, "description": description, "status": "pending"}),
        encoding="utf-8")


def test_extract_directives_splits_imperatives_and_skips_the_rest():
    text = ("Please make sure the summary keeps it. Also add a check. Thanks for the update. "
            "Is this right? I want the list per turn. We shipped it.")
    got = PD.extract_directives(text)
    assert got == ["Please make sure the summary keeps it.", "Also add a check.",
                   "I want the list per turn."]
    assert PD.extract_directives("ok") == []
    assert PD.extract_directives(None) == []


def test_turn_20_fixture_with_no_task_row_is_two_unmapped_entries(session):
    out = PD.unmapped_directives(SID)
    assert [(d["turn"], d["index"]) for d in out] == [(20, 0), (20, 1)]
    assert all(d["mapped_to"] is None for d in out)
    assert "make sure" in out[0]["text"] and "add a check" in out[1]["text"]


def test_op_returns_unmapped_and_mapped_split(session):
    out = PD._pm_directive_unmapped({"session_id": SID})
    assert len(out["unmapped"]) == 2 and out["mapped"] == []


def test_task_citing_the_turn_maps_one_directive_only_per_cite(session):
    _, home = session
    _task(home, "1", "Handle PM turn 20 asks")
    assert PD.unmapped_directives(SID) == []
    _task(home, "1", "Handle PM turn 2 asks")
    assert len(PD.unmapped_directives(SID)) == 2


def test_task_sharing_content_words_maps_that_directive(session):
    _, home = session
    _task(home, "1", "Add check flagging each directive without a task row")
    out = PD.unmapped_directives(SID)
    assert [d["index"] for d in out] == [0]


def test_disposition_row_maps_and_plan_ref_is_reported(session):
    r = PD._pm_directive_dispose({"session_id": SID, "turn": 20, "index": 0,
                                  "disposition": "planned",
                                  "ref": "plan:docs/plans/x.md#C0"})
    assert r["ok"] is True
    entries = {d["index"]: d for d in PD.map_directives(SID)}
    assert entries[0]["mapped_to"] == "disposition:plan:docs/plans/x.md#C0"
    assert entries[1]["mapped_to"] is None


def test_dispose_refuses_bad_params_and_missing_session_dir(session, tmp_path, monkeypatch):
    assert PD._pm_directive_dispose({"session_id": SID, "turn": "x", "index": 0,
                                     "disposition": "d"})["ok"] is False
    assert PD._pm_directive_dispose({"session_id": SID, "turn": 1, "index": 0,
                                     "disposition": " "})["ok"] is False
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "absent" / s)
    assert PD.append_disposition(SID, 1, 0, "d")["ok"] is False
    assert not (tmp_path / "absent").exists()


def test_sessionend_op_writes_both_and_never_advises(session):
    sdir, _ = session
    out = PD._sessionend_pm_directive_check({"payload": {"session_id": SID, "cwd": ""}})
    assert "decision" not in json.dumps(out)
    data = json.loads((sdir / PD.UNMAPPED_FILENAME).read_text(encoding="utf-8"))
    assert [(d["turn"], d["index"]) for d in data["unmapped"]] == [(20, 0), (20, 1)]


def test_sessionend_op_missing_session_dir_records_nothing(session, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "gone" / s)
    PD._sessionend_pm_directive_check({"payload": {"session_id": SID}})
    assert not (tmp_path / "gone").exists()
    PD._sessionend_pm_directive_check({"payload": {}})


def test_no_turns_yields_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / s)
    assert PD.map_directives("nope") == []

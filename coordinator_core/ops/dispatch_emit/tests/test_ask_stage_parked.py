"""dispatch.ask_stage answers parked, staging nothing, for a run carrying a stop_after=plan marker."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import PARKED_REPLY_KEY, STOP_AFTER_FILE, record_dir
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN, PLAN_REL, read_stage_reply

_RUN = "parkrun"


def _seed(root) -> None:
    (root / ".git").mkdir(exist_ok=True)
    (root / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")


def _marker(root, text: str):
    path = record_dir(root, _RUN) / STOP_AFTER_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _params() -> dict:
    return {"run_id": _RUN, "plan_path": PLAN_REL, "writes": []}


def test_marker_parks_without_staging(tmp_path, monkeypatch):
    _seed(tmp_path)
    _marker(tmp_path, json.dumps({"stop_after": "plan", "at": "2026-10-21T00:00:00Z", "from": None}))
    monkeypatch.setattr(ask_stage, "stage", lambda *a, **k: (_ for _ in ()).throw(AssertionError("staged")))

    reply = _handler(_params(), repo_root=tmp_path)

    assert reply == {PARKED_REPLY_KEY: "plan", "plan_path": PLAN_REL}
    assert not (record_dir(tmp_path, _RUN) / "manifest.json").exists()


def test_parked_reply_plan_path_none_when_absent(tmp_path):
    (tmp_path / ".git").mkdir()
    _marker(tmp_path, json.dumps({"stop_after": "plan"}))

    reply = _handler({"run_id": _RUN, "sizing_path": "x.yaml"}, repo_root=tmp_path)

    assert reply == {PARKED_REPLY_KEY: "plan", "plan_path": None}


def test_absent_marker_stages_as_today(tmp_path):
    _seed(tmp_path)

    reply = _handler(_params(), repo_root=tmp_path)

    assert PARKED_REPLY_KEY not in reply and "error" not in reply
    assert read_stage_reply(tmp_path, reply)["rows"]


def test_malformed_marker_stages_as_today(tmp_path):
    _seed(tmp_path)
    marker = _marker(tmp_path, "{not json")

    reply = _handler(_params(), repo_root=tmp_path)

    assert PARKED_REPLY_KEY not in reply and "error" not in reply
    assert marker.exists()


def test_other_stop_value_stages_as_today(tmp_path):
    _seed(tmp_path)
    _marker(tmp_path, json.dumps({"stop_after": "review"}))

    reply = _handler(_params(), repo_root=tmp_path)

    assert PARKED_REPLY_KEY not in reply


def test_marker_holds_until_the_park_is_recorded(tmp_path):
    _seed(tmp_path)
    marker = _marker(tmp_path, json.dumps({"stop_after": "plan"}))

    first = _handler(_params(), repo_root=tmp_path)
    assert first[PARKED_REPLY_KEY] == "plan" and marker.exists()

    # The parked reply was lost: nothing recorded the park, so the run parks again.
    again = _handler(_params(), repo_root=tmp_path)
    assert again[PARKED_REPLY_KEY] == "plan" and marker.exists()

    (record_dir(tmp_path, _RUN) / "halted.json").write_text(json.dumps({"halted": "parked"}), encoding="utf-8")
    resumed = _handler(_params(), repo_root=tmp_path)
    assert PARKED_REPLY_KEY not in resumed and "error" not in resumed and not marker.exists()
    assert (record_dir(tmp_path, _RUN) / "manifest.json").is_file()

"""The compact-only `objective_resume` SessionStart leg re-injects a bound run's objective.json."""

from __future__ import annotations

import asyncio
import json

import pytest

from coordinator_core import warp_live
from coordinator_core.hooks import sessionstart_dispatch as sd

RUN_ID = "warp-test-run"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    run_dir = tmp_path / "state" / "warp" / RUN_ID
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(warp_live, "live_run_for_session", lambda root, sid, **kw: RUN_ID)
    return tmp_path


def _write_objective(repo):
    run_dir = repo / "state" / "warp" / RUN_ID
    (run_dir / "objective.json").write_text(
        json.dumps({"verbatim": "ship the thing", "axes": [{"id": "a1"}, {"id": "a2"}]}),
        encoding="utf-8",
    )
    loop = [
        {"kind": "check", "axes": [{"axis": "a1", "pass": False}, {"axis": "a2", "pass": False}]},
        {"kind": "check", "axes": [{"axis": "a1", "pass": True}, {"axis": "a2", "pass": False}]},
        {"kind": "reacher", "axes": [{"axis": "a1", "pass": False}]},
    ]
    (run_dir / "objective-loop.jsonl").write_text(
        "\n".join(json.dumps(x) for x in loop) + "\n", encoding="utf-8"
    )
    return run_dir / "objective.json"


def _payload(repo, source):
    return {"source": source, "session_id": "s1", "cwd": str(repo)}


def test_leg_is_registered_for_compact_only():
    assert [s for k, s, _ in sd._LEGS if k == "objective_resume"] == [frozenset({"compact"})]


def test_present_injects_promise_axes_path_and_last_check_status(repo):
    path = _write_objective(repo)
    text = sd._leg_objective_resume(_payload(repo, "compact"))
    assert "ship the thing" in text
    assert str(path) in text
    assert "a1 [pass]" in text
    assert "a2 [not_met]" in text


def test_present_without_loop_marks_axes_unchecked(repo):
    path = _write_objective(repo)
    (path.parent / "objective-loop.jsonl").unlink()
    text = sd._leg_objective_resume(_payload(repo, "compact"))
    assert "a1 [unchecked]" in text


def test_absent_file_injects_nothing(repo):
    assert sd._leg_objective_resume(_payload(repo, "compact")) is None


def test_unbound_session_injects_nothing(repo, monkeypatch):
    _write_objective(repo)
    monkeypatch.setattr(warp_live, "live_run_for_session", lambda root, sid, **kw: None)
    assert sd._leg_objective_resume(_payload(repo, "compact")) is None


def _stub_other_legs(monkeypatch):
    for _k, _s, fn_name in sd._LEGS:
        if fn_name != "_leg_objective_resume":
            monkeypatch.setattr(sd, fn_name, lambda payload: None)


def test_handler_injects_on_compact_not_on_other_sources(repo, monkeypatch):
    _write_objective(repo)
    _stub_other_legs(monkeypatch)

    def run(source):
        res = asyncio.run(sd._handler({"payload": _payload(repo, source)}))
        return res.get("hookSpecificOutput", {}).get("additionalContext", "")

    assert "ship the thing" in run("compact")
    for source in ("startup", "resume", "clear", "fork"):
        assert "ship the thing" not in run(source)

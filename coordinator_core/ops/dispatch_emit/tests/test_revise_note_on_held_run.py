"""workflow.revise: a note on a held, never-emitted run is queued for its first emit."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

HELD = "ask-20261030T094206-e52b1f"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(rl, "_parent_lists_run", lambda root, run_id: run_id == HELD)
    return tmp_path


def test_note_on_held_run_replies_ok_and_is_carried(repo):
    out = rl._handler({"run_id": HELD, "note": "findings: use repo-first"}, repo_root=repo)
    assert out["ok"] is True
    assert "queued for the run's start" in out["note"]
    assert [e["note"] for e in rl.carried_notes(repo, HELD)] == ["findings: use repo-first"]
    assert rl.read_run(rl.run_dir(repo, HELD)) is None


def test_verbatim_on_held_run_still_queues(repo):
    out = rl._handler({"run_id": HELD, "verbatim": "also do X"}, repo_root=repo)
    assert out["ok"] is True and out["item"] == "Q1"
    assert rl.carried_notes(repo, HELD) == []


def test_note_and_verbatim_together_both_land(repo):
    out = rl._handler({"run_id": HELD, "verbatim": "do Y", "note": "ctx"}, repo_root=repo)
    assert out["ok"] is True and out["item"] == "Q1"
    assert "queued for the run's start" in out["note"]
    assert [e["note"] for e in rl.carried_notes(repo, HELD)] == ["ctx"]


def test_note_on_unknown_run_still_refuses_and_creates_no_dir(repo):
    out = rl._handler({"run_id": "ask-unknown", "note": "n"}, repo_root=repo)
    assert out.get("ok") is not True and "unknown run" in out["error"]
    assert not rl.run_dir(repo, "ask-unknown").exists()


def test_other_kinds_on_held_run_stay_refused(repo):
    out = rl._handler({"run_id": HELD, "note": "n", "stop_after": rl.STOP_AFTER_PLAN}, repo_root=repo)
    assert out.get("ok") is not True and "emit it first" in out["error"]
    assert rl.carried_notes(repo, HELD) == []

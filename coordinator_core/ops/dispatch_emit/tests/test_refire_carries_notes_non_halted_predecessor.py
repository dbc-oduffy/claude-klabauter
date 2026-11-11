"""A refire carries the live revise notes of the newest prior run of its sizing, whatever state that run ended in."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.test_refire_carries_notes_every_mint_path import (  # noqa: F401
    LIVE_A,
    LIVE_B,
    _fire,
    _seed_notes,
    _spy_texts,
)
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    ASK,
    REL,
    WARP,
    composed,
    repo,
)

NEWER_WARP = "warp-2026-10-16-refire-c3b-20271231T000000-eeeeee"


def _prior(repo, run_id, status=None):
    rl.start(repo, run_id)
    rl.bind(repo, run_id, REL, ask_text="the ask")
    if status:
        (record_dir(repo, run_id) / halted_run.HALTED_FILE).write_text(
            json.dumps({"status": status, "sizing_rel": REL}), encoding="utf-8"
        )


def _text(seen):
    return seen["script"] + "\n".join((seen["kw"].get("staged_out") or {}).values())


def test_a_non_halted_ask_predecessor_carries_its_notes(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _prior(repo, ASK, status="closed")
    _seed_notes(repo, ASK)
    assert halted_run.halted_run_to_resume(repo, REL) is None
    reply = _fire(repo)
    assert reply["run_id"] != ASK
    assert rl.read_run(rl.run_dir(repo, reply["run_id"]))[rl.RESUMED_FROM_FIELD] == ASK
    assert f"> {LIVE_A}" in _text(seen) and f"> {LIVE_B}" in _text(seen)


def test_a_warp_predecessor_superseded_by_a_newer_non_halted_run_carries_its_notes(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _prior(repo, WARP, status="resumed")
    _seed_notes(repo, WARP)
    _prior(repo, NEWER_WARP)
    # A staged run with a live owner is no orphan (`orphan_verdict` "owner-live"): the gate never adopts it,
    # so the refire mints fresh and carries WARP's notes. An ownerless staged run is adopted instead.
    (record_dir(repo, NEWER_WARP) / "run-stage.json").write_text(
        json.dumps({"stage": "staged", "session_id": "live-owner-session"}), encoding="utf-8"
    )
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid: sid == "live-owner-session")
    assert halted_run.halted_run_to_resume(repo, REL) is None
    reply = _fire(repo)
    assert reply["run_id"] not in (WARP, NEWER_WARP)
    assert rl.read_run(rl.run_dir(repo, reply["run_id"]))[rl.RESUMED_FROM_FIELD] == WARP
    assert f"> {LIVE_A}" in _text(seen)


def test_the_newest_note_holder_wins_and_a_noteless_sizing_resumes_nothing(repo):  # noqa: F811
    _prior(repo, ASK, status="closed")
    assert halted_run.notes_predecessor(repo, REL) is None
    _seed_notes(repo, ASK)
    assert halted_run.notes_predecessor(repo, REL) == ASK
    assert halted_run.notes_predecessor(repo, REL, exclude=ASK) is None
    _prior(repo, WARP)
    _seed_notes(repo, WARP)
    assert halted_run.notes_predecessor(repo, REL) == ASK
    assert halted_run.notes_predecessor(repo, "state/sizings/2026-10-16-other.yaml") is None
    reply = _fire(repo)
    assert rl.RESUMED_FROM_FIELD in rl.read_run(rl.run_dir(repo, reply["run_id"]))

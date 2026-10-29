"""A refire whose rebind is blocked records the halted warp- run as resumed_from and renders its notes."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import OUTPUT_PATH_REDIRECT_KEY
from coordinator_core.ops.dispatch_emit.tests.test_refire_resumes_halted_run import (  # noqa: F401
    HALTED_BASE,
    HEAD,
    OLDER,
    _emit_by,
    _fire,
    _halt,
    _marker,
    repo,
)

NOTE = "C0 must also write its test"
NOTE_AT = "2026-10-22T00:20:00Z"


def _blocked_fire(repo, monkeypatch):
    import coordinator_core.git.git_state as git_state

    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: HEAD)
    _halt(repo, run_base_sha=HALTED_BASE)
    (rl.run_dir(repo, OLDER) / rl.NOTES_FILE).write_text(
        json.dumps({"note": NOTE, "at": NOTE_AT}) + "\n", encoding="utf-8"
    )
    _emit_by(repo, "dead-peer", False, monkeypatch)
    return _fire(repo)


def test_a_blocked_rebind_records_the_halted_run_as_resumed_from(repo, monkeypatch):
    reply = _blocked_fire(repo, monkeypatch)
    assert reply[OUTPUT_PATH_REDIRECT_KEY]["halted_run"] == OLDER
    assert reply["run_id"] != OLDER
    run = rl.read_run(rl.run_dir(repo, reply["run_id"]))
    assert run[rl.RESUMED_FROM_FIELD] == OLDER


def test_a_blocked_rebind_renders_the_halted_runs_note(repo, monkeypatch):
    reply = _blocked_fire(repo, monkeypatch)
    view = (rl.run_dir(repo, reply["run_id"]) / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert NOTE in view


def test_a_blocked_rebind_leaves_the_halted_marker_halted(repo, monkeypatch):
    _blocked_fire(repo, monkeypatch)
    assert _marker(repo)["status"] != "resumed"


def test_an_unblocked_rebind_records_no_resumed_from(repo, monkeypatch):
    _halt(repo)
    assert _fire(repo)["run_id"] == OLDER
    assert (rl.read_run(rl.run_dir(repo, OLDER)) or {}).get(rl.RESUMED_FROM_FIELD) is None


def _abandon(repo, run_id):
    return op._dispatch_emit({"abandon_run": run_id, "target_root": str(repo)}, repo)


def test_abandoning_a_recomposed_run_nothing_started_closes_it(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_run as hr
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    reply = _blocked_fire(repo, monkeypatch)
    rid = reply["run_id"]
    assert _abandon(repo, rid)["abandoned"] is True
    assert hr._read(record_dir(repo, rid))["status"] == hr.STATUS_CLOSED


def test_abandon_leaves_a_run_without_resumed_from_alone(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_run as hr
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    _halt(repo)
    assert _fire(repo)["run_id"] == OLDER
    assert _abandon(repo, OLDER)["abandoned"] is False
    assert hr._read(record_dir(repo, OLDER))["status"] != hr.STATUS_CLOSED

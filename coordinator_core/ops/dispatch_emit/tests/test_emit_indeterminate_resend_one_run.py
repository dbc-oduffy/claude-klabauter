"""A dispatch.emit whose reply is lost (-32004) and re-sent yields exactly one run; the client names the re-send."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import launch_records, warp_live
from coordinator_core.ops.dispatch_emit import op as emit_op
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_seed import repo  # noqa: F401
from coordinator_core.warm import client

ASK = "add the lost-reply thing"


@pytest.fixture
def sessions(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("sessions")
    managed, legacy = base / "managed", base / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base / "claude"))
    return managed, base


def _run_dirs(root):
    return sorted(p.name for p in (root / RECORD_DIR_ROOT).iterdir() if p.is_dir())


def _fire(root, run_id, sessions):
    """Make the minted run read live: a submission record plus a launch record for its script."""
    managed, base = sessions
    script = run_token._read_rec(root, run_id)["script_path"]
    Path(launch_records.record_path(str(managed), "s1", "w1")).write_text(
        json.dumps({"scriptPath": script, "fired_at": 1.0}), encoding="utf-8"
    )
    transcript = base / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    warp_live.record_submission(
        root, run_id, session_id="s1", transcript_path=str(transcript), script_path=script,
        sizing_rel=None, tool_use_id="tu1",
    )


def test_a_resent_raw_ask_after_a_lost_reply_returns_the_one_live_run(repo, sessions):
    first = emit_op._dispatch_emit({"ask": ASK}, repo)
    assert first["run_id"] and _run_dirs(repo) == [first["run_id"]]
    _fire(repo, first["run_id"], sessions)

    resent = emit_op._dispatch_emit({"ask": ASK}, repo)

    assert resent["run_id"] == first["run_id"]
    assert resent["deduped"] is True
    assert _run_dirs(repo) == [first["run_id"]]


def test_a_different_ask_still_mints_its_own_run(repo, sessions):
    first = emit_op._dispatch_emit({"ask": ASK}, repo)
    _fire(repo, first["run_id"], sessions)

    other = emit_op._dispatch_emit({"ask": "an unrelated ask"}, repo)

    assert other["run_id"] != first["run_id"]
    assert "deduped" not in other
    assert len(_run_dirs(repo)) == 2


def test_a_lost_reply_resend_returns_the_minted_unfired_run(repo, sessions):
    first = emit_op._dispatch_emit({"ask": ASK}, repo)

    resent = emit_op._dispatch_emit({"ask": ASK}, repo)

    assert resent["run_id"] == first["run_id"]
    assert resent["deduped"] is True
    assert resent["sha256"] == first["sha256"]
    assert _run_dirs(repo) == [first["run_id"]]


def test_an_unfired_run_past_the_window_is_not_resent_and_mints_afresh(repo, sessions, monkeypatch):
    first = emit_op._dispatch_emit({"ask": ASK}, repo)
    monkeypatch.setattr(emit_op, "_UNFIRED_RESEND_WINDOW_SECS", -1.0)

    again = emit_op._dispatch_emit({"ask": ASK}, repo)

    assert again["run_id"] != first["run_id"]
    assert "deduped" not in again


def test_the_sizing_is_the_key_when_present(repo, monkeypatch):
    rec = {"run_id": "warp-live-1", "sizing_rel": "state/sizings/x.yaml", "minted_at": 1.0, "ask_sha256": "other"}
    monkeypatch.setattr(run_token, "held_records", lambda root: [rec])
    monkeypatch.setattr(warp_live, "live_run_for_sizing", lambda root, rel, **_k: "warp-live-1")
    monkeypatch.setattr(warp_live, "run_for_ask", lambda *_a, **_k: pytest.fail("a sizing keys by sizing"))

    reply = emit_op._resent_live_emit({}, repo, "state/sizings/x.yaml", ASK)

    assert reply["run_id"] == "warp-live-1" and reply["deduped"] is True


def test_a_reserved_run_emit_keeps_its_own_live_run_refusal(repo, monkeypatch):
    monkeypatch.setattr(warp_live, "live_run_for_sizing", lambda *_a, **_k: pytest.fail("not consulted"))

    assert emit_op._resent_live_emit({"reserved_run_id": "r1"}, repo, "state/sizings/x.yaml", None) is None


def test_the_indeterminate_envelope_for_emit_names_the_resend_and_not_started():
    msg = {"jsonrpc": "2.0", "id": 7, "method": "dispatch.emit", "params": {"ask": ASK}}

    envelope = client._indeterminate_envelope(msg, "no response within 5s", dispatch_key="9-1")

    error = envelope["error"]
    assert error["code"] == client.WARM_DISPATCH_INDETERMINATE
    assert "re-send" in error["message"] and "NOT STARTED" in error["message"]
    assert error["data"]["resend_returns_existing_run"] is True
    assert error["data"]["reconcile"] == "warm.request_status"


def test_other_mutating_ops_do_not_claim_a_resend_is_safe():
    msg = {"jsonrpc": "2.0", "id": 7, "method": "some.mutating.op", "params": {}}

    envelope = client._indeterminate_envelope(msg, "no response within 5s")

    assert "resend_returns_existing_run" not in envelope["error"]["data"]
    assert "re-send" not in envelope["error"]["message"]


def test_the_door_still_never_replays_an_emit():
    assert "dispatch.emit" not in client._COLD_REPLAY_SAFE

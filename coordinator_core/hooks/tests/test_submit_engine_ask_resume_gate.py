"""The hook's resume route refuses a cancelled, superseded or closed run and a foreign driver (absent
`args.takeover`), logs a takeover and refreshes the run token, and lets the same or an unrecorded driver through."""

from __future__ import annotations

import json

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_fresh_refire import RUN, _marker, _payload, _repo
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.resume_gate import REFUSE_FOREIGN_DRIVER, TAKEOVER_LOG, ResumeRefused, check_resume

pytestmark = pytest.mark.complete

from coordinator_core.hooks.tests.test_submit_engine_ask_fresh_refire import SIZING_REL


def _drive(root, monkeypatch, session, **args):
    monkeypatch.setattr(hrr, "engine_sha", lambda: None)
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: True)
    minted: list = []
    monkeypatch.setattr(mod, "_ensure_launcher_run_token", lambda r, run_id, s, sess: minted.append(sess))
    payload = _payload(root, **args)
    payload["session_id"] = session
    params = {"session_id": session, "ask": True, "sizing_path": SIZING_REL}
    envelope, result = mod._resume_route(root, SIZING_REL, payload, params, session, None, retired=[])
    return envelope, result, minted


def _stage(root, session):
    (root / RECORD_DIR_ROOT / RUN / "run-stage.json").write_text(
        json.dumps({"stage": "commit", "session_id": session}), encoding="utf-8")


def _refusal(envelope) -> str:
    out = envelope["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


def _set_marker(root, **fields):
    path = root / RECORD_DIR_ROOT / RUN / "halted.json"
    path.write_text(json.dumps({**_marker(root), **fields}), encoding="utf-8")


def test_a_cancelled_run_is_refused_with_the_fresh_alternative(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=None)
    _set_marker(root, halted="cancelled")
    envelope, result, _ = _drive(root, monkeypatch, "s1")
    assert result is None and "cancelled" in _refusal(envelope) and "fresh" in _refusal(envelope)


@pytest.mark.parametrize("status", ["superseded", "closed"])
def test_a_superseded_or_closed_run_is_refused(tmp_path, status):
    root = _repo(tmp_path, stamp=None)
    _set_marker(root, status=status)
    with pytest.raises(ResumeRefused, match=status) as exc:
        check_resume(root, RUN, "s1", None)
    assert "fresh" in str(exc.value)


def test_a_foreign_driver_is_refused_without_takeover(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=None)
    _stage(root, "other")
    envelope, _, minted = _drive(root, monkeypatch, "s1")
    assert "takeover" in _refusal(envelope) and "other" in _refusal(envelope)
    assert minted == [] and not (root / RECORD_DIR_ROOT / RUN / TAKEOVER_LOG).exists()
    assert REFUSE_FOREIGN_DRIVER == "resume-foreign-driver"


def test_a_foreign_driver_with_takeover_resumes_and_logs(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=None)
    _stage(root, "other")
    envelope, result, minted = _drive(root, monkeypatch, "s1", takeover={"reason": "peer died"})
    assert envelope is not None or result is not None
    assert "s1" in minted
    rows = [json.loads(l) for l in (root / RECORD_DIR_ROOT / RUN / TAKEOVER_LOG).read_text().splitlines()]
    assert len(rows) == 1
    assert (rows[0]["from_session"], rows[0]["to_session"], rows[0]["reason"]) == ("other", "s1", "peer died")
    assert rows[0]["at"]


def test_an_empty_takeover_reason_does_not_take_over(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=None)
    _stage(root, "other")
    envelope, _, _ = _drive(root, monkeypatch, "s1", takeover={"reason": "  "})
    assert "takeover" in _refusal(envelope)


def test_the_same_driver_resumes_without_a_log(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=None)
    _stage(root, "s1")
    envelope, result, minted = _drive(root, monkeypatch, "s1")
    assert envelope is not None or result is not None
    assert not (root / RECORD_DIR_ROOT / RUN / TAKEOVER_LOG).exists()


def test_an_unrecorded_driver_is_not_refused(tmp_path, monkeypatch):
    root = _repo(tmp_path, stamp=None)
    envelope, result, _ = _drive(root, monkeypatch, "s1")
    assert envelope is not None or result is not None


def test_takeover_is_an_accepted_key_on_both_surfaces():
    from coordinator_core.ops import engine_ask_submit

    assert "takeover" in mod._ARGS_KEYS and "takeover" in engine_ask_submit._KNOWN_KEYS

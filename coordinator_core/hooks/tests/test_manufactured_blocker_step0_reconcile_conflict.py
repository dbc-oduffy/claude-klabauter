"""The skill-mandated /workday-start Step 0 RECONCILE-CONFLICT A/B/C ask is
never a manufactured blocker; the same hand-up wording without that outcome
in the turn still fires."""

from __future__ import annotations

import json

from coordinator_core.hooks import guard_manufactured_blocker as gmb

ASK = "Reconcile hit a conflict on main. Option A, B or C? This needs you to pick."


def _write(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result(text):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}],
        },
    }


def _assistant(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _run(tmp_path, monkeypatch, entries):
    transcript = tmp_path / "t.jsonl"
    _write(transcript, entries)
    monkeypatch.setattr(gmb, "_has_fired_this_session", lambda payload: False)
    monkeypatch.setattr(gmb, "_mark_fired_this_session", lambda payload: None)
    monkeypatch.setattr(gmb, "_auto_discharge_fire", lambda payload: None)
    monkeypatch.setattr(gmb, "resolve_posture", lambda root: "precision")
    payload = {"transcript_path": str(transcript), "session_id": "s1"}
    return gmb._handler({"payload": payload, **payload})


def test_step0_reconcile_conflict_ask_is_not_flagged(tmp_path, monkeypatch):
    out = _run(
        tmp_path,
        monkeypatch,
        [
            _user("/workday-start"),
            _tool_result("RECONCILE-CONFLICT branch=work/x\n"),
            _assistant(ASK),
        ],
    )
    assert not out


def test_same_ask_without_step0_outcome_still_fires(tmp_path, monkeypatch):
    out = _run(tmp_path, monkeypatch, [_user("go"), _assistant(ASK)])
    assert out


def test_prior_turn_reconcile_conflict_does_not_exempt(tmp_path, monkeypatch):
    out = _run(
        tmp_path,
        monkeypatch,
        [
            _user("/workday-start"),
            _tool_result("RECONCILE-CONFLICT branch=work/x\n"),
            _assistant("resolved"),
            _user("next"),
            _assistant(ASK),
        ],
    )
    assert out

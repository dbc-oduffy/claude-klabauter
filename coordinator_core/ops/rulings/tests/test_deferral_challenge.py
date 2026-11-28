"""deferral.challenge dispatched through coordinator_core.ipc: verdicts, flag_em, queue append, never denies."""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION
from coordinator_core.deny_registry import GUARD_DENY_CLASS, PM_RULED_EXCEPTIONS
from coordinator_core.ops.rulings import challenge  # noqa: F401  (registers the op)


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)

    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    return tmp_path


def _call(root, params):
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "deferral.challenge",
        "params": params,
        "_origin_worktree": str(root),
    }
    return asyncio.run(ipc.dispatch_message(msg))


def _queue(root, session):
    return root / ".coordinator-local" / "em-flags" / f"{session}.jsonl"


def test_unnamed_deferral_challenges_flags_and_appends(root):
    reply = _call(
        root,
        {
            "surface": "reviewer_unapplied",
            "text": "Not now; follow-up later.",
            "fields": {"unapplied_reason": "not now"},
            "session_id": "s1",
        },
    )
    assert "error" not in reply, reply
    result = reply["result"]
    assert result["flag_em"] is True
    first = result["verdicts"][0]
    assert first["verdict"] == "challenge" and first["reason_class"] is None
    lines = _queue(root, "s1").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["surface"] == "reviewer_unapplied"
    assert record["challenges"] and all(c["verdict"] == "challenge" for c in record["challenges"])
    assert result["flag_ref"]["line"] == 1
    assert result["flag_ref"]["path"].endswith("s1.jsonl")


def test_substantiated_named_reason_passes_and_writes_nothing(root):
    reply = _call(
        root,
        {
            "surface": "reviewer_unapplied",
            "text": "",
            "fields": {"unapplied_reason": "another repo: coordinator-content-repo owns hook wiring"},
            "session_id": "s2",
        },
    )
    assert "error" not in reply, reply
    result = reply["result"]
    assert result["flag_em"] is False
    assert "flag_ref" not in result
    assert [(v["verdict"], v["reason_class"]) for v in result["verdicts"]] == [("pass", "another_repo")]
    assert not _queue(root, "s2").exists()


def test_bare_plan_sized_in_agent_report_challenges(root):
    reply = _call(
        root,
        {"surface": "agent_report", "text": "Left unfixed: plan-sized.", "session_id": "s3"},
    )
    result = reply["result"]
    assert result["flag_em"] is True
    assert any(v["verdict"] == "challenge" for v in result["verdicts"])


def test_missing_session_id_queues_unattributed(root):
    reply = _call(root, {"surface": "halt_record", "text": "Not now; follow-up later."})
    assert "error" not in reply, reply
    assert reply["result"]["flag_em"] is True
    assert _queue(root, "_unattributed").is_file()


def test_second_challenge_appends_second_line(root):
    params = {"surface": "agent_report", "text": "Not now.", "session_id": "s4"}
    _call(root, params)
    reply = _call(root, params)
    assert reply["result"]["flag_ref"]["line"] == 2
    assert len(_queue(root, "s4").read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize(
    "text",
    ["", "   ", "\x00\x01 not now ퟿", "x" * 50000, "Not now. " * 2000],
)
def test_never_denies_on_any_content(root, text):
    reply = _call(root, {"surface": "plan", "text": text, "fields": {"deferrals": [None, 3, {}]}, "session_id": "s5"})
    assert "error" not in reply, reply
    assert isinstance(reply["result"]["flag_em"], bool)


def test_unknown_surface_is_a_caller_error_not_a_write(root):
    reply = _call(root, {"surface": "chat", "text": "Not now.", "session_id": "s6"})
    assert "error" in reply
    assert not _queue(root, "s6").exists()


def test_classified_mutating_with_no_deny_entry():
    assert OP_CLASSIFICATION["deferral.challenge"].name == "MUTATING"
    assert "deferral.challenge" not in GUARD_DENY_CLASS
    assert "deferral.challenge" not in PM_RULED_EXCEPTIONS

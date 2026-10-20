"""A fired `claude -p` child is a new session; the fire token, not its id, proves ownership."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from coordinator_core.hooks import allow_emitted_workflow_fire as allow
from coordinator_core.hooks import block_workflow_foreign_emission as bwfe
from coordinator_core.ops.workflow_fire.receipt import FIRE_TOKEN_ENV, stamp_fire_ownership

_EMITTER = "sess-emitter"
_TOKEN = "tok-" + "a" * 28


def _emit(tmp_path: Path) -> Path:
    script = tmp_path / "p.workflow.mjs"
    script.write_text("// emitted\n", encoding="utf-8")
    receipt = {
        "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "session_id": _EMITTER,
        "plan": "p.md",
    }
    Path(str(script) + ".emitted.json").write_text(json.dumps(receipt), encoding="utf-8")
    return script


def _call(handler, script: Path, session: str, env: dict | None = None) -> dict:
    params = {
        "tool_name": "Workflow",
        "tool_input": {"scriptPath": str(script)},
        "session_id": session,
        "cwd": str(script.parent),
    }
    if env is not None:
        params["env"] = env
    return handler(params)


def test_fired_child_with_the_token_is_admitted(tmp_path):
    script = _emit(tmp_path)
    assert stamp_fire_ownership(script, "fire1", _TOKEN)
    assert _call(bwfe._handler, script, "sess-child", {FIRE_TOKEN_ENV: _TOKEN}) == {}


def test_unrelated_session_without_the_token_is_still_denied(tmp_path):
    script = _emit(tmp_path)
    stamp_fire_ownership(script, "fire1", _TOKEN)
    hso = _call(bwfe._handler, script, "sess-peer", {})["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "DIFFERENT session" in hso["permissionDecisionReason"]


def test_a_wrong_token_is_denied(tmp_path):
    script = _emit(tmp_path)
    stamp_fire_ownership(script, "fire1", _TOKEN)
    hso = _call(bwfe._handler, script, "sess-peer", {FIRE_TOKEN_ENV: "guess"})["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_token_without_a_fire_stamp_is_denied(tmp_path):
    script = _emit(tmp_path)
    hso = _call(bwfe._handler, script, "sess-peer", {FIRE_TOKEN_ENV: _TOKEN})["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_the_token_does_not_excuse_an_edited_script(tmp_path):
    script = _emit(tmp_path)
    stamp_fire_ownership(script, "fire1", _TOKEN)
    with script.open("ab") as fh:
        fh.write(b"\n")
    hso = _call(bwfe._handler, script, "sess-child", {FIRE_TOKEN_ENV: _TOKEN})["hookSpecificOutput"]
    assert "sha" in hso["permissionDecisionReason"].lower()


def test_receipt_stores_the_digest_never_the_token(tmp_path):
    script = _emit(tmp_path)
    stamp_fire_ownership(script, "fire1", _TOKEN)
    assert _TOKEN not in Path(str(script) + ".emitted.json").read_text(encoding="utf-8")


def test_allow_hook_admits_the_fired_child_only_with_the_token(tmp_path):
    script = _emit(tmp_path)
    stamp_fire_ownership(script, "fire1", _TOKEN)
    ok = _call(allow._handler, script, "sess-child", {FIRE_TOKEN_ENV: _TOKEN})
    assert ok.get("hookSpecificOutput", {}).get("permissionDecision") == "allow"
    assert _call(allow._handler, script, "sess-peer", {}) == {}

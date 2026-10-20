"""Contract pin for `block_workflow_foreign_emission.evaluate`, the forwarder seam."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from coordinator_core.frontmatter.primitives import canonical_body_sha
from coordinator_core.hooks import block_workflow_foreign_emission as bwfe
from coordinator_core.ops.workflow_fire.receipt import FIRE_TOKEN_ENV, stamp_fire_ownership

_TOKEN = "tok-" + "b" * 28
_UNTYPED = 'agent({ prompt: "do it" });\n'


def _emit(tmp_path: Path, text: str = "// emitted\n", **extra) -> Path:
    script = tmp_path / "p.workflow.mjs"
    script.write_text(text, encoding="utf-8")
    receipt = {
        "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "session_id": "sess-emitter",
        **extra,
    }
    Path(str(script) + ".emitted.json").write_text(json.dumps(receipt), encoding="utf-8")
    return script


def _payload(script: Path | None = None, session: str = "sess-emitter", **tool_input) -> dict:
    if script is not None:
        tool_input["scriptPath"] = str(script)
    return {
        "tool_name": "Workflow",
        "tool_input": tool_input,
        "session_id": session,
        "cwd": str(script.parent) if script else ".",
    }


def _reason(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


def test_signature_is_pinned():
    sig = inspect.signature(bwfe.evaluate)
    assert list(sig.parameters) == ["payload", "env", "repo_root"]
    assert sig.parameters["env"].default is None
    assert sig.parameters["repo_root"].default is None


def test_read_only_exemption_admits(tmp_path):
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"script": 'agent({ prompt: "x", agentType: "Explore" });'},
    }
    assert bwfe.evaluate(payload, {}) == {}


def test_no_receipt_deny(tmp_path):
    script = tmp_path / "hand.workflow.mjs"
    script.write_text(_UNTYPED, encoding="utf-8")
    assert "no receipt" in _reason(bwfe.evaluate(_payload(script), {}))


def test_sha_mismatch_deny(tmp_path):
    script = _emit(tmp_path)
    script.write_text("// edited\n", encoding="utf-8")
    assert "changed after emission" in _reason(bwfe.evaluate(_payload(script), {}))


def test_session_mismatch_deny(tmp_path):
    script = _emit(tmp_path)
    assert "DIFFERENT session" in _reason(bwfe.evaluate(_payload(script, "sess-peer"), {}))


def test_fire_token_admit_comes_from_env_argument(tmp_path):
    script = _emit(tmp_path)
    assert stamp_fire_ownership(script, "fire1", _TOKEN)
    assert bwfe.evaluate(_payload(script, "sess-child"), {FIRE_TOKEN_ENV: _TOKEN}) == {}
    assert "DIFFERENT session" in _reason(bwfe.evaluate(_payload(script, "sess-child"), {}))


def test_env_defaults_to_os_environ(tmp_path, monkeypatch):
    script = _emit(tmp_path)
    stamp_fire_ownership(script, "fire1", _TOKEN)
    monkeypatch.setenv(FIRE_TOKEN_ENV, _TOKEN)
    assert bwfe.evaluate(_payload(script, "sess-child")) == {}
    monkeypatch.delenv(FIRE_TOKEN_ENV)
    _reason(bwfe.evaluate(_payload(script, "sess-child")))


def test_plan_amended_deny(tmp_path):
    (tmp_path / ".git").mkdir()
    plan = tmp_path / "plan.md"
    plan.write_text("---\ntitle: t\n---\n\nbody one\n", encoding="utf-8")
    recorded = canonical_body_sha(plan.read_text(encoding="utf-8"))
    script = _emit(tmp_path, plan_body_sha=recorded, plan_relpath="plan.md")
    assert bwfe.evaluate(_payload(script), {}) == {}
    plan.write_text("---\ntitle: t\n---\n\nbody two\n", encoding="utf-8")
    assert "plan body that changed" in _reason(bwfe.evaluate(_payload(script), {}))


def test_resume_check_runs_only_for_a_resume_fire(tmp_path, monkeypatch):
    script = _emit(tmp_path)
    monkeypatch.setattr(bwfe, "_resume_after_revise_reason", lambda s: "revised after halt")
    assert bwfe.evaluate(_payload(script), {}) == {}
    assert _reason(bwfe.evaluate(_payload(script, resumeFromRunId="r1"), {})) .endswith("revised after halt")


def test_fails_open_on_internal_error(tmp_path, monkeypatch):
    script = _emit(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(bwfe, "_session_id", boom)
    assert bwfe.evaluate(_payload(script), {}) == {}


@pytest.mark.parametrize("case", ["readonly", "no_receipt", "sha", "session", "token", "plan"])
def test_handler_and_evaluate_agree(tmp_path, case):
    if case == "readonly":
        payload = {"tool_name": "Workflow", "tool_input": {"script": "const x = 1;"}}
        env: dict = {}
    else:
        script = _emit(tmp_path)
        env = {}
        sess = "sess-emitter"
        if case == "no_receipt":
            Path(str(script) + ".emitted.json").unlink()
            script.write_text(_UNTYPED, encoding="utf-8")
        elif case == "sha":
            script.write_text("// edited\n", encoding="utf-8")
        elif case == "session":
            sess = "sess-peer"
        elif case == "token":
            stamp_fire_ownership(script, "fire1", _TOKEN)
            sess, env = "sess-child", {FIRE_TOKEN_ENV: _TOKEN}
        elif case == "plan":
            (tmp_path / ".git").mkdir()
            (tmp_path / "plan.md").write_text("a\n", encoding="utf-8")
            _emit(tmp_path, plan_body_sha="0" * 64, plan_relpath="plan.md")
        payload = _payload(script, sess)
    expected = bwfe.evaluate(payload, env)
    assert bwfe._handler({**payload, "env": env}) == expected


@pytest.fixture
def _warn_level(monkeypatch):
    monkeypatch.setattr(bwfe, "apply_guard_level", lambda name, env, **kw: {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                               "additionalContext": "Advisory: warn"}})


def test_post_emit_edit_denies_even_at_warn_level(tmp_path, _warn_level):
    script = _emit(tmp_path)
    script.write_text("// edited after emit\n", encoding="utf-8")
    _reason(bwfe.evaluate(_payload(script), env={}))


def test_peer_session_script_denies_even_at_warn_level(tmp_path, _warn_level):
    script = _emit(tmp_path)
    _reason(bwfe.evaluate(_payload(script, session="sess-peer"), env={}))


def test_no_receipt_inline_script_follows_the_guard_level(tmp_path, _warn_level):
    out = bwfe.evaluate({"tool_name": "Workflow", "tool_input": {"script": _UNTYPED}}, env={})
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

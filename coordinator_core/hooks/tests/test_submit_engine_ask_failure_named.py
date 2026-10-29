"""An exception escaping evaluate denies naming its class and message; a retire failure rides the rewrite reason."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.hooks import submit_engine_ask as mod


def _params(tmp_path: Path, args: dict) -> dict:
    return {
        "tool_name": "Workflow",
        "cwd": str(tmp_path),
        "session_id": "s1",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": args},
    }


def _hso(env: dict) -> dict:
    return env["hookSpecificOutput"]


def test_exception_in_evaluate_denies_naming_class_and_message(tmp_path, monkeypatch):
    def boom(_payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "evaluate", boom)
    monkeypatch.setattr("coordinator_core.machine_profile.apply_guard_level", lambda _name, env: env)
    env = mod._handler(_params(tmp_path, {"ask": "x"}))
    assert _hso(env)["permissionDecision"] == "deny"
    reason = _hso(env)["permissionDecisionReason"]
    assert "RuntimeError: boom" in reason
    assert "is down" not in reason


def test_refusal_denies_at_every_guard_level_with_its_reason(tmp_path, monkeypatch):
    from coordinator_core import machine_profile as mp

    monkeypatch.setattr(mod, "evaluate", lambda _p: mod._refuse("repo X is another checkout."))
    for level in ("strict", "warn", "off"):
        monkeypatch.setattr(mp, "guard_level", lambda _name, level=level: level)
        env = mod._handler(_params(tmp_path, {"ask": "x"}))
        assert _hso(env)["permissionDecision"] == "deny", level
        assert "another checkout" in _hso(env)["permissionDecisionReason"], level


def test_other_guard_still_lowers_at_warn(monkeypatch):
    from coordinator_core import machine_profile as mp

    monkeypatch.setattr(mp, "guard_level", lambda _name: "warn")
    env = mp.apply_guard_level("some-ordinary-guard", mod._refuse("nope."))
    assert _hso(env)["permissionDecision"] == "allow"


def test_non_submission_still_passes_through_the_handler(tmp_path):
    params = _params(tmp_path, {"ask": "x"})
    params["tool_name"] = "Bash"
    env = mod._handler(params)
    assert env.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def _submit_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)


def test_emit_reply_with_no_run_and_no_words_is_denied_naming_the_cause(tmp_path):
    _submit_root(tmp_path)
    for reply in ({}, {"ok": True}, None):
        env = mod.evaluate(_params(tmp_path, {"ask": "x"}), emit=lambda _p, _r, reply=reply: reply)
        reason = _hso(env)["permissionDecisionReason"]
        assert _hso(env)["permissionDecision"] == "deny"
        cause = reason.split("emitted no run:")[1].split("\nInstead")[0].strip()
        assert cause and cause != "None", reply


def test_emit_timeout_with_empty_message_names_its_class(tmp_path):
    _submit_root(tmp_path)

    def emit(_p, _r):
        raise TimeoutError()

    env = mod.evaluate(_params(tmp_path, {"ask": "x"}), emit=emit)
    assert _hso(env)["permissionDecision"] == "deny"
    assert "refused the submission: TimeoutError" in _hso(env)["permissionDecisionReason"]


def test_emit_reply_with_findings_still_names_them(tmp_path):
    _submit_root(tmp_path)
    env = mod.evaluate(_params(tmp_path, {"ask": "x"}), emit=lambda _p, _r: {"ok": False, "findings": "bad sizing"})
    assert "bad sizing" in _hso(env)["permissionDecisionReason"]


def test_handler_never_returns_empty_for_a_submission(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "evaluate", lambda _p: {})
    monkeypatch.setattr("coordinator_core.machine_profile.apply_guard_level", lambda _name, env: None)
    env = mod._handler(_params(tmp_path, {"ask": "x"}))
    assert _hso(env)["permissionDecision"] == "deny"
    assert _hso(env)["permissionDecisionReason"].strip()


def test_retire_failure_is_named_in_the_rewrite_and_does_not_block(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    sizing = "state/sizings/x.yaml"
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / sizing).write_text("{}\n", encoding="utf-8")

    def bad_retire(_root, _rel):
        raise OSError("disk gone")

    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.ask_gate.retire_token_spent_halt", bad_retire)
    monkeypatch.setattr(
        "coordinator_core.warp_live.record_submission", lambda *a, **k: None
    )
    script = tmp_path / "run.js"
    script.write_text("", encoding="utf-8")
    def emit(_p, _r):
        return {"path": str(script), "run_id": "r1", "ok": True}

    env = mod.evaluate(_params(tmp_path, {"sizing": sizing}), emit=emit)
    hso = _hso(env)
    assert hso["permissionDecision"] == "allow"
    assert "OSError: disk gone" in hso["additionalContext"]
    assert hso["updatedInput"]["scriptPath"] == str(script)

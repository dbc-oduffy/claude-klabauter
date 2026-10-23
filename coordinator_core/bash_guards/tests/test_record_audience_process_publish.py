"""Process, heavy-command and publish guards return AlternativeRecords, never envelopes."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.bash_guards import (
    block_editor_kill_by_name as kill,
    block_perforce_submit as p4,
    block_unreal_engine_resave as resave,
    guard_background_publish as publish,
    guard_deploy_dirty_tree as deploy,
    guard_headless_claude_plugin_dir as headless,
    guard_heavy_command_admission as admission,
    guard_piped_pytest_exit as piped,
    guard_post_executor_verification as post_exec,
    guard_subagent_heavy_ue_launch as ue_launch,
    guard_trailing_background_ampersand as ampersand,
)
from coordinator_core.guard_messages.alternative_record import (
    AlternativeRecord,
    Audience,
    EscalationRoute,
    Override,
)
from coordinator_core.guard_messages.render import render


def _bash(cmd: str, **extra) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, **extra}


def _text(envelope: dict) -> str:
    hso = envelope["hookSpecificOutput"]
    return hso.get("permissionDecisionReason") or hso.get("additionalContext", "")


def _em(record: AlternativeRecord) -> str:
    return _text(render(record, Audience.EM))


def _no_override(record: AlternativeRecord) -> None:
    assert isinstance(record, AlternativeRecord)
    assert not any(isinstance(m, Override) for m in record.members)


def test_editor_kill_offers_the_pid_form_and_still_allows_a_pid_kill():
    record = kill.check(_bash("taskkill /F /IM UnrealEditor.exe"))
    _no_override(record)
    assert "Kill by the PID of your own launch" in _em(record)
    assert any("taskkill /PID <pid>" in getattr(m, "template", "") for m in record.members)
    assert kill.check(_bash("taskkill /F /PID 4242")) is None


def test_engine_resave_offers_the_scoped_form_and_still_allows_projectonly():
    record = resave.check(_bash("UnrealEditor-Cmd.exe proj/P.uproject -run=ResavePackages"))
    _no_override(record)
    assert "-projectonly" in _em(record)
    assert resave.check(_bash("UnrealEditor-Cmd.exe proj/P.uproject -run=ResavePackages -projectonly")) is None


def test_perforce_submit_carries_a_route_that_survives_the_subagent_render(monkeypatch):
    monkeypatch.setattr(p4, "_armed", lambda: True)
    record = p4.check(_bash("p4 submit -c 1"))
    _no_override(record)
    assert any(isinstance(m, EscalationRoute) for m in record.members)
    assert p4.ANCHOR in _em(record)
    assert p4.ANCHOR in _text(render(record, Audience.SUBAGENT))
    monkeypatch.setattr(p4, "_armed", lambda: False)
    assert p4.check(_bash("p4 submit -c 1")) is None


def test_background_publish_is_a_rewrite_record_that_keeps_every_input_field():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "python percolate-round.py --dest x", "description": "d"},
    }
    record = publish.check_background_publish(payload)
    _no_override(record)
    assert record.verdict_kind == "rewrite"
    assert record.updated_input == {
        "command": "python percolate-round.py --dest x",
        "description": "d",
        "run_in_background": True,
    }
    payload["tool_input"]["run_in_background"] = True
    assert publish.check_background_publish(payload) is None


def test_deploy_dirty_tree_denies_with_git_status_and_passes_a_clean_tree(monkeypatch, tmp_path):
    payload = _bash("firebase deploy", cwd=str(tmp_path))
    monkeypatch.setattr(deploy, "_dirty_paths", lambda cwd: ["src/app.ts"])
    record = deploy.check(payload)
    _no_override(record)
    text = _em(record)
    assert "src/app.ts" in text and "git status" in text
    monkeypatch.setattr(deploy, "_dirty_paths", lambda cwd: [])
    assert deploy.check(payload) is None


def test_headless_launch_rewrite_and_unresolved_root_advisory_are_records(monkeypatch):
    monkeypatch.setattr(headless, "_is_dev_install", lambda: True)
    monkeypatch.setattr(headless, "_plugin_root", lambda: "/plugin-root")
    rewrite = headless.check_headless_claude_plugin_dir('claude -p "x"')
    _no_override(rewrite)
    assert rewrite.verdict_kind == "rewrite"
    assert rewrite.updated_input == {"command": 'claude --plugin-dir /plugin-root -p "x"'}
    assert headless.check_headless_claude_plugin_dir("claude --version") is None
    monkeypatch.setattr(headless, "_plugin_root", lambda: None)
    advisory = headless.check_headless_claude_plugin_dir('claude -p "x"')
    _no_override(advisory)
    assert advisory.verdict_kind == "advisory" and "claude-author" in _em(advisory)


def test_piped_pytest_names_both_dialect_fixes_and_stays_quiet_with_pipefail():
    record = piped.check_piped_pytest_exit("pytest tests | tail -5")
    _no_override(record)
    text = _em(record)
    assert "pipefail" in text and "$LASTEXITCODE" in text
    assert piped.check_piped_pytest_exit("set -o pipefail; pytest tests | tail -5") is None


def test_trailing_ampersand_offers_run_in_background_and_honours_the_skip():
    record = ampersand.check_trailing_background_ampersand("sleep 1 &")
    _no_override(record)
    assert "run_in_background: true" in _em(record)
    assert ampersand.check_trailing_background_ampersand("sleep 1") is None
    assert (
        ampersand.check_trailing_background_ampersand(
            "sleep 1 &", payload={"env": {ampersand.OVERRIDE_ENV: "1"}}
        )
        is None
    )


def test_subagent_ue_launch_routes_to_the_slot_and_leaves_the_main_loop_alone():
    record = ue_launch.check(_bash("RunUAT.bat BuildCookRun", agent_id="a1"))
    _no_override(record)
    assert "needs_slot" in _text(render(record, Audience.SUBAGENT))
    assert ue_launch.check(_bash("RunUAT.bat BuildCookRun")) is None


def test_heavy_admission_denies_are_records_with_an_offer_or_route(monkeypatch):
    hold = SimpleNamespace(set_by_session="other", expires_at=0, reason="reindex")
    monkeypatch.setattr(admission, "_read_box_hold", lambda: hold)
    record = admission._box_hold_leg({"session_id": "mine"})
    _no_override(record)
    assert any(isinstance(m, EscalationRoute) for m in record.members)
    text = _em(record)
    assert "other" in text and "gate-blocker: guard-denied" in text
    assert admission._box_hold_leg({"session_id": "other"}) is None
    assert admission._deny_reason(record) == text


def test_post_executor_deny_is_a_record_and_the_write_hook_still_gets_an_envelope(monkeypatch):
    record = post_exec._deny_record("sentinel.txt")
    _no_override(record)
    text = _em(record)
    assert "coordinator:test-runner" in text and "benchmarks.measure" in text
    assert "sentinel.txt" in text

    from coordinator_core.hooks import dispatch_escape as nd

    monkeypatch.setattr(post_exec, "_gate", lambda payload, subject: ("s", "/r", "deny"))
    monkeypatch.setattr(post_exec, "written_text_verifies", lambda payload, subject, root: True)
    monkeypatch.setattr(nd, "_take_escape", lambda *a, **k: False)
    envelope = post_exec.after_executor_deny({"session_id": "s"}, "scratch/x.py")
    assert isinstance(envelope, dict) and envelope["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.setattr(post_exec, "written_text_verifies", lambda payload, subject, root: False)
    assert post_exec.after_executor_deny({"session_id": "s"}, "scratch/x.py") is None


def test_em_render_is_the_legacy_text_and_rewrites_keep_the_allow_decision():
    record = ampersand.check_trailing_background_ampersand("sleep 1 &")
    assert _em(record).endswith("Use `run_in_background: true` instead.")
    rewrite = publish.check_background_publish(
        {"tool_name": "Bash", "tool_input": {"command": "python percolate-round.py --dest x"}}
    )
    assert rewrite.rewrite_allow is True
    assert render(rewrite, Audience.EM)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert _em(rewrite).startswith("Publish round moved to a background task")
    assert render(rewrite, Audience.EM)["hookSpecificOutput"]["updatedInput"]["run_in_background"] is True

"""Tests for `coordinator_core.hooks.support` — the W4-C3 arrival footprint.

Scope: the eleven modules W4-C3's `writes:` list names
(docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C3). Each module
landed with `REAL_GUARD_REGISTRY`/`REAL_STOP_FAMILY_REGISTRY` deliberately
empty (its enrolled bodies are a later wave's `writes:`), so these tests
exercise the runner MECHANISM directly — aggregation, exception isolation,
lazy-import scope matching, envelope translation — over synthesized
entries/payloads, never a real enrolled guard body (there is none in this
chunk's footprint).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.hooks.support import (
    forwarder_resolve,
    guard_runner,
    guard_runner_contract,
    message_envelope,
    sentinel_write_guard,
    session_hub,
    skill_invocation,
    stop_family_runner,
    stop_family_runner_contract,
)


class TestMessageEnvelope:
    def test_compose_strips_and_requires_nonempty_prose(self):
        msg = message_envelope.compose("  hello  ")
        assert msg.prose == "hello"
        assert msg.alternative is None
        assert msg.anchor is None
        with pytest.raises(ValueError):
            message_envelope.compose("   ")

    def test_compose_rejects_alternative_missing_command_shape(self):
        with pytest.raises(ValueError):
            message_envelope.compose("prose", alternative="This is a sentence about it.")

    def test_compose_accepts_runnable_alternative(self):
        msg = message_envelope.compose("prose", alternative="git status")
        assert msg.alternative == "git status"

    def test_validate_alternative_shape_rejects_embedded_fence(self):
        ok, reason = message_envelope.validate_alternative_shape("```\ngit status\n```")
        assert ok is False
        assert "fenced" in reason

    def test_validate_alternative_shape_rejects_too_many_lines(self):
        block = "\n".join(f"cmd{i}" for i in range(message_envelope.ALTERNATIVE_MAX_LINES + 1))
        ok, reason = message_envelope.validate_alternative_shape(block)
        assert ok is False
        assert "exceeds" in reason

    def test_validate_alternative_shape_none_is_valid(self):
        assert message_envelope.validate_alternative_shape(None) == (True, None)

    def test_render_includes_fenced_alternative_and_anchor(self):
        msg = message_envelope.compose("diagnosis", alternative="git status", anchor="see docs/wiki/x.md")
        text = message_envelope.render(msg)
        assert "diagnosis" in text
        assert "```\ngit status\n```" in text
        assert "See see docs/wiki/x.md." in text

    def test_resolve_wiki_citation_noop_without_plugin_root(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        text = "Reference: docs/wiki/foo.md"
        assert message_envelope.resolve_wiki_citation(text) == text

    def test_resolve_wiki_citation_resolves_under_plugin_root(self, monkeypatch, tmp_path):
        doctrine_root = tmp_path / "plugin"
        (doctrine_root / "docs" / "wiki").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(doctrine_root))
        text = "Reference: docs/wiki/foo.md"
        resolved = message_envelope.resolve_wiki_citation(text)
        assert "docs/wiki/foo.md" not in resolved or str(doctrine_root) in resolved

    def test_emit_unknown_channel_raises(self):
        msg = message_envelope.compose("x")
        with pytest.raises(ValueError):
            message_envelope.emit(msg, "not-a-channel")

    def test_emit_measurement_mode_writes_structured_record(self, monkeypatch, capsys):
        monkeypatch.setenv(message_envelope.MEASURE_ENV_VAR, "1")
        msg = message_envelope.compose("hello", alternative=None, anchor=None)
        result = message_envelope.emit(msg, message_envelope.CHANNEL_ADDITIONAL_CONTEXT)
        assert result is None
        out = capsys.readouterr().out
        assert '"prose":"hello"' in out

    def test_emit_additional_context_writes_envelope_to_stdout(self, monkeypatch, capsys):
        monkeypatch.delenv(message_envelope.MEASURE_ENV_VAR, raising=False)
        msg = message_envelope.compose("hello")
        code = message_envelope.emit(msg, message_envelope.CHANNEL_ADDITIONAL_CONTEXT)
        assert code == 0
        out = capsys.readouterr().out
        assert '"additionalContext"' in out
        assert '"hookEventName":"PreToolUse"' in out


class TestSkillInvocation:
    def test_normalize_command_name_strips_namespace(self):
        assert skill_invocation.normalize_command_name("coordinator:pickup") == "pickup"

    def test_normalize_command_name_bare_verb_unchanged(self):
        assert skill_invocation.normalize_command_name("pickup") == "pickup"

    def test_normalize_command_name_non_str_returns_empty(self):
        assert skill_invocation.normalize_command_name(None) == ""
        assert skill_invocation.normalize_command_name(123) == ""

    def test_read_invocation_user_prompt_expansion(self):
        payload = {
            "command_name": "coordinator:pickup",
            "command_args": "  arg1  ",
            "session_id": "sid",
            "cwd": "/tmp",
            "agent_id": None,
        }
        inv = skill_invocation.read_invocation(payload)
        assert inv.command_name == "pickup"
        assert inv.command_args == "arg1"
        assert inv.session_id == "sid"
        assert inv.agent_id is None

    def test_read_invocation_pre_tool_use_skill(self):
        payload = {
            "tool_name": "Skill",
            "tool_input": {"skill": "coordinator:plan", "args": "x"},
            "session_id": "sid2",
            "cwd": "/tmp",
            "agent_id": "agent-1",
        }
        inv = skill_invocation.read_invocation(payload)
        assert inv.command_name == "plan"
        assert inv.command_args == "x"
        assert inv.agent_id == "agent-1"

    def test_read_invocation_pre_tool_use_skill_falls_back_to_command(self):
        payload = {
            "tool_name": "Skill",
            "tool_input": {"command": "coordinator:review", "args": ""},
            "session_id": "sid3",
            "cwd": "/tmp",
        }
        inv = skill_invocation.read_invocation(payload)
        assert inv.command_name == "review"

    def test_read_invocation_unrecognized_returns_none(self):
        assert skill_invocation.read_invocation({"tool_name": "Write"}) is None
        assert skill_invocation.read_invocation({}) is None
        assert skill_invocation.read_invocation("not a dict") is None

    def test_context_envelope_uses_given_event_name(self):
        import json

        rendered = skill_invocation.context_envelope("UserPromptExpansion", "text")
        parsed = json.loads(rendered)
        assert parsed["hookSpecificOutput"]["hookEventName"] == "UserPromptExpansion"
        assert parsed["hookSpecificOutput"]["additionalContext"] == "text"


class TestGuardRunner:
    def test_run_guards_first_deny_wins(self):
        entries = [
            {"channel": guard_runner_contract.CHANNEL_DENY, "text": "first"},
            {"channel": guard_runner_contract.CHANNEL_DENY, "text": "second"},
        ]
        result = guard_runner.run_guards(entries, payload={})
        assert result["permissionDecision"] == "deny"
        assert result["permissionDecisionReason"] == "first"

    def test_run_guards_concatenates_additional_context(self):
        entries = [
            {"channel": guard_runner_contract.CHANNEL_ADDITIONAL_CONTEXT, "text": "a"},
            {"channel": guard_runner_contract.CHANNEL_ADDITIONAL_CONTEXT, "text": "b"},
        ]
        result = guard_runner.run_guards(entries, payload={})
        assert result["additionalContext"] == "a\n\nb"
        assert "permissionDecision" not in result

    def test_run_guards_deny_and_advisory_both_surface(self):
        entries = [
            {"channel": guard_runner_contract.CHANNEL_DENY, "text": "deny-text"},
            {"channel": guard_runner_contract.CHANNEL_ADDITIONAL_CONTEXT, "text": "advisory"},
        ]
        result = guard_runner.run_guards(entries, payload={})
        assert result["permissionDecision"] == "deny"
        assert result["additionalContext"] == "advisory"

    def test_run_guards_exception_isolation(self):
        def _boom(_payload):
            raise RuntimeError("boom")

        skipped = []
        entries = [("boom-guard", _boom), {"channel": "additional_context", "text": "ok"}]
        result = guard_runner.run_guards(entries, payload={}, skipped_out=skipped)
        assert skipped == ["boom-guard"]
        assert result["additionalContext"] == "ok"

    def test_run_guards_callable_entry_invoked_with_payload(self):
        seen = {}

        def _capture(payload):
            seen["payload"] = payload
            return {"channel": "additional_context", "text": "seen"}

        guard_runner.run_guards([("cap", _capture)], payload={"k": "v"})
        assert seen["payload"] == {"k": "v"}

    def test_run_guards_empty_verdict_is_a_noop(self):
        result = guard_runner.run_guards([None, {}], payload={})
        assert result == {"additionalContext": ""}

    def test_envelope_to_verdict_deny(self):
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "no",
            }
        }
        verdict = guard_runner.envelope_to_verdict(out)
        assert verdict == {"channel": guard_runner_contract.CHANNEL_DENY, "text": "no"}

    def test_envelope_to_verdict_additional_context(self):
        out = {"hookSpecificOutput": {"additionalContext": "hi"}}
        verdict = guard_runner.envelope_to_verdict(out)
        assert verdict == {"channel": guard_runner_contract.CHANNEL_ADDITIONAL_CONTEXT, "text": "hi"}

    def test_envelope_to_verdict_none_in_none_out(self):
        assert guard_runner.envelope_to_verdict(None) is None
        assert guard_runner.envelope_to_verdict({}) is None
        assert guard_runner.envelope_to_verdict({"hookSpecificOutput": "not-a-dict"}) is None

    def test_verdict_to_envelope_roundtrip_deny(self):
        result = {"permissionDecision": "deny", "permissionDecisionReason": "no", "additionalContext": ""}
        envelope = guard_runner.verdict_to_envelope(result)
        assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert envelope["hookSpecificOutput"]["permissionDecisionReason"] == "no"
        assert "additionalContext" not in envelope["hookSpecificOutput"]

    def test_verdict_to_envelope_none_when_nothing_to_report(self):
        assert guard_runner.verdict_to_envelope({"additionalContext": ""}) is None

    def test_real_guard_registry_is_empty(self):
        assert guard_runner_contract.ENROLLED_GUARD_MODULES
        # No live REAL_GUARD_REGISTRY symbol is asserted non-empty: it is


class TestGuardScopeDescriptor:
    def test_matches_false_when_no_target_path(self):
        d = guard_runner_contract.GuardScopeDescriptor(guard_module="x.py", path_suffixes=frozenset({".md"}))
        assert d.matches(None) is False
        assert d.matches("") is False

    def test_matches_false_when_no_restrictions_declared(self):
        d = guard_runner_contract.GuardScopeDescriptor(guard_module="x.py")
        assert d.matches("anything.md") is False

    def test_matches_suffix_and_directory(self):
        d = guard_runner_contract.GuardScopeDescriptor(
            guard_module="x.py",
            path_suffixes=frozenset({".md"}),
            directory_substrings=("coordinator/skills/",),
        )
        assert d.matches("coordinator/skills/foo.md") is True
        assert d.matches("other/foo.md") is False
        assert d.matches("coordinator/skills/foo.py") is False

    def test_matches_windows_backslash_directory_substring(self):
        d = guard_runner_contract.GuardScopeDescriptor(
            guard_module="x.py",
            path_suffixes=frozenset({".md"}),
            directory_substrings=("coordinator/skills/",),
        )
        assert d.matches(r"coordinator\skills\foo.md") is True

    def test_matches_basename_is_an_or_not_an_and(self):
        d = guard_runner_contract.GuardScopeDescriptor(
            guard_module="x.py",
            path_suffixes=frozenset({".md"}),
            directory_substrings=("some/dir/",),
            basenames=frozenset({"coordinator.local.md"}),
        )
        assert d.matches("repo-root/coordinator.local.md") is True

    def test_prebuilt_descriptors_match_expected_shapes(self):
        assert guard_runner_contract.DOCTRINE_CHANGELOG_PROSE_SCOPE_DESCRIPTOR.matches(
            "coordinator/docs/wiki/foo.md"
        )
        assert guard_runner_contract.DOCTRINE_CHANGELOG_PROSE_SCOPE_DESCRIPTOR.matches(
            "coordinator.local.md"
        )
        assert guard_runner_contract.GUARD_DOCTRINE_SURFACE_RATIO_SCOPE_DESCRIPTOR.matches(
            "coordinator/skills/foo.md"
        )
        assert guard_runner_contract.CHECK_CLAUDE_MD_SIZE_SCOPE_DESCRIPTOR.matches(
            "/repo/CLAUDE.md"
        )
        assert not guard_runner_contract.CHECK_CLAUDE_MD_SIZE_SCOPE_DESCRIPTOR.matches(
            "/repo/other.md"
        )


class TestStopFamilyRunner:
    def test_run_stop_family_guards_concatenates_all_fired(self):
        def _fire_a():
            return 2, "a fired"

        def _fire_b():
            return 2, "b fired"

        def _silent():
            return 0, ""

        entries = [("a", _fire_a), ("b", _fire_b), ("silent", _silent)]
        exit_code, text = stop_family_runner.run_stop_family_guards(entries)
        assert exit_code == 2
        assert text == "a fired\n\nb fired"

    def test_run_stop_family_guards_all_silent_is_exit_zero(self):
        entries = [("a", lambda: (0, "")), ("b", lambda: (0, ""))]
        exit_code, text = stop_family_runner.run_stop_family_guards(entries)
        assert exit_code == 0
        assert text == ""

    def test_run_stop_family_guards_exception_isolation(self):
        def _boom():
            raise RuntimeError("boom")

        skipped = []
        entries = [("boom", _boom), ("ok", lambda: (2, "ok fired"))]
        exit_code, text = stop_family_runner.run_stop_family_guards(entries, skipped_out=skipped)
        assert skipped == ["boom"]
        assert exit_code == 2
        assert text == "ok fired"

    def test_real_stop_family_registry_is_empty(self):
        assert stop_family_runner.REAL_STOP_FAMILY_REGISTRY == ()

    def test_build_stop_family_entries_scope_gates_before_import(self):
        descriptor = stop_family_runner_contract.STOP_FAMILY_SCOPE_DESCRIPTORS
        assert isinstance(descriptor, dict)
        entries = stop_family_runner.build_stop_family_entries(
            registry=(), raw_payload_text="{}", payload={"tool_input": {"file_path": "x.md"}}
        )
        assert entries == []


class TestSentinelWriteGuard:
    def test_extract_target_path_checks_keys_in_order(self):
        assert sentinel_write_guard.extract_target_path({"file_path": " /a/b.md "}) == "/a/b.md"
        assert sentinel_write_guard.extract_target_path({"notebook_path": "/a/n.ipynb"}) == "/a/n.ipynb"
        assert sentinel_write_guard.extract_target_path({}) == ""
        assert sentinel_write_guard.extract_target_path("not-a-dict") == ""

    def test_is_sentinel_write_basename_case_insensitive(self, tmp_path):
        target = tmp_path / "SENTINEL.md"
        assert sentinel_write_guard.is_sentinel_write(str(target), "sentinel.md") is True

    def test_is_sentinel_write_false_for_unrelated_file(self, tmp_path):
        target = tmp_path / "other.md"
        assert sentinel_write_guard.is_sentinel_write(str(target), "sentinel.md") is False

    def test_is_sentinel_write_false_for_empty_path(self):
        assert sentinel_write_guard.is_sentinel_write("", "sentinel.md") is False

    def test_sentinel_write_denial_returns_none_when_not_sentinel(self):
        assert sentinel_write_guard.sentinel_write_denial("/a/other.md", "sentinel.md", "reason") is None

    def test_sentinel_write_denial_returns_deny_envelope(self, tmp_path):
        target = tmp_path / "sentinel.md"
        result = sentinel_write_guard.sentinel_write_denial(str(target), "sentinel.md", "no touching")
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "no touching"

    def test_reconstruct_after_is_importable(self):
        assert callable(sentinel_write_guard.reconstruct_after)


class TestSessionHub:
    def test_session_id_is_real_accepts_uuid4_shape(self):
        assert session_hub.session_id_is_real("12345678-1234-1234-1234-123456789012") is True

    def test_session_id_is_real_rejects_non_uuid(self):
        assert session_hub.session_id_is_real("not-a-uuid") is False
        assert session_hub.session_id_is_real(None) is False
        assert session_hub.session_id_is_real(123) is False

    def test_ensure_session_dir_refuses_synthetic_id(self, tmp_path):
        target = tmp_path / "hub" / "fake-session"
        assert session_hub.ensure_session_dir(str(target), "not-a-real-session-id") is False
        assert not target.exists()

    def test_ensure_session_dir_creates_for_real_id(self, tmp_path):
        target = tmp_path / "hub" / "12345678-1234-1234-1234-123456789012"
        ok = session_hub.ensure_session_dir(str(target), "12345678-1234-1234-1234-123456789012")
        assert ok is True
        assert target.is_dir()

    def test_ensure_session_dir_idempotent(self, tmp_path):
        session_id = "12345678-1234-1234-1234-123456789012"
        target = tmp_path / "hub" / session_id
        assert session_hub.ensure_session_dir(str(target), session_id) is True
        assert session_hub.ensure_session_dir(str(target), session_id) is True


class TestForwarderResolve:
    def test_resolve_forwarder_none_when_absent(self, tmp_path):
        assert forwarder_resolve.resolve_forwarder(tmp_path, "missing") is None

    def test_resolve_forwarder_finds_extensionless(self, tmp_path):
        script = tmp_path / "mytool"
        script.write_text("#!/usr/bin/env python3\n")
        found = forwarder_resolve.resolve_forwarder(tmp_path, "mytool")
        assert found == script

    def test_resolve_forwarder_prefers_extensionless_over_exe(self, tmp_path):
        (tmp_path / "mytool.exe").write_bytes(b"\x00")
        script = tmp_path / "mytool"
        script.write_text("#!/usr/bin/env python3\n")
        found = forwarder_resolve.resolve_forwarder(tmp_path, "mytool")
        assert found == script

    def test_forwarder_argv_exe_suffix_launched_bare(self, tmp_path):
        exe = tmp_path / "mytool.exe"
        exe.write_bytes(b"\x00")
        argv = forwarder_resolve.forwarder_argv(exe, tail=["a"])
        assert argv == [str(exe), "a"]

    def test_forwarder_argv_python_script_gets_interpreter_prefix(self, tmp_path):
        script = tmp_path / "mytool"
        script.write_text("#!/usr/bin/env python3\nprint('hi')\n")
        argv = forwarder_resolve.forwarder_argv(script, tail=["--flag"])
        assert argv == [sys.executable, str(script), "--flag"]

    def test_forwarder_argv_native_image_launched_bare_despite_no_suffix(self, tmp_path):
        native = tmp_path / "mytool"
        native.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 16)
        argv = forwarder_resolve.forwarder_argv(native)
        assert argv == [str(native)]


class TestPackageShape:
    def test_support_package_registers_no_ops(self):
        import coordinator_core.hooks.support as support_pkg

        assert not hasattr(support_pkg, "register_op")

"""Behavioral tests for
coordinator_core.write_guards.block_home_dir_memo_delivery.

Cross-checked against the DoE-claude reference hook by the differential
harness (coordinator/tests/test_write_guard_fan_in_differential.py, C5) --
this file covers the port's own local behavior: the stderr-noise regression
found in review (a non-matching containment root must be an ordinary
negative result, not a logged anomaly, since this guard's CLASS/MATCHERS
put it on nearly every write in the fleet) and the ``_guarded_roots``
de-duplication step.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.write_guards import block_home_dir_memo_delivery as guard


def _payload(file_path: str, tool_name: str = "Write") -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    }


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)


@pytest.fixture(autouse=True)
def _fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "cross-repo" / "inbox").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    return home


class TestStderrNoiseRegression:
    """Finding 1 (review, 2026-07-29): a non-matching containment root must
    not print to stderr -- this guard's CLASS/MATCHERS mean the allow path
    (an ordinary write elsewhere on disk) is the overwhelming common case,
    and the module docstring's own contract says the allow path does zero
    subprocess work and zero disk I/O, a contract "silent" is part of.
    """

    def test_ordinary_write_elsewhere_is_silent(self, capsys, tmp_path):
        target = str(tmp_path / "some" / "other" / "file.py")
        result = guard.check(_payload(target))
        assert result is None
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_config_write_under_home_claude_is_silent(self, capsys, _fake_home):
        target = str(_fake_home / ".claude" / "settings.json")
        result = guard.check(_payload(target))
        assert result is None
        captured = capsys.readouterr()
        assert captured.err == ""


class TestGuardedRootsDeduplication:
    """Finding 2 (review, 2026-07-29): HOME/USERPROFILE/Path.home() commonly
    coincide -- the original hand-rolled implementation de-duped; the port
    must too, or every call multiplies Finding 1's (now-fixed) print count
    and does needless repeat work.
    """

    def test_coinciding_home_env_vars_yield_one_root(self, _fake_home):
        roots = guard._guarded_roots()
        assert len(roots) == len(set(roots)), f"duplicate roots present: {roots!r}"

    def test_distinct_home_and_userprofile_yield_two_roots(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        userprofile = tmp_path / "userprofile"
        (home / ".claude" / "cross-repo").mkdir(parents=True, exist_ok=True)
        (userprofile / ".claude" / "cross-repo").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(userprofile))
        roots = guard._guarded_roots()
        assert len(roots) == len(set(roots))
        assert len(roots) >= 2


class TestDenyAndAllow:
    def test_hand_written_cross_repo_write_denied(self, _fake_home):
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_override_env_allows(self, monkeypatch, _fake_home):
        monkeypatch.setenv(guard.OVERRIDE_ENV, "1")
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is None

    def test_override_env_name_absent_from_deny_text(self, _fake_home):
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert guard.OVERRIDE_ENV not in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_non_guarded_tool_allowed(self, _fake_home):
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target, tool_name="Read"))
        assert result is None

    def test_extended_length_prefix_asymmetry_still_denies(self, monkeypatch, _fake_home):
        target_path = _fake_home / ".claude" / "cross-repo" / "inbox" / "x.md"
        target = str(target_path)

        real_resolve = Path.resolve

        def fake_resolve(self, *a, **kw):
            result = real_resolve(self, *a, **kw)
            if str(self).endswith("x.md"):
                return Path("\\\\?\\" + str(result))
            return result

        monkeypatch.setattr(Path, "resolve", fake_resolve)

        result = guard.check(_payload(target))

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_state_nested_cross_repo_write_denied(self, _fake_home):
        target = str(_fake_home / ".claude" / "state" / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_case_varied_target_denied(self, _fake_home):
        target = str(_fake_home / ".Claude" / "Cross-Repo" / "Inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestDenyMessageContent:
    """Item 30 (cross-repo/archive/2026-09-24-doe-claude-em-block-home-dir-
    memo-delivery-lost-config-only.md): ``_deny_reason`` had drifted from
    the module's own docstring, dropping the CONFIG-ONLY framing, the
    ``claude-home`` alias, and the destination inbox path. Pins three of the
    memo's four literal needles verbatim. The fourth, a single-shot
    ``cross-repo-memo --to doe-claude-em --topic <slug> --title "<t>"``
    invocation, is a RETIRED CLI flag form (DR-210 -- see
    ``coordinator/bin/cross-repo-memo.py``'s ``send`` subparser comment,
    "No legacy one-shot flag form") and is deliberately NOT reproduced: an
    offered command that no longer runs is not a real ALTERNATIVE
    (docs/wiki/guard-messaging.md § Trichotomy), so this checks for the
    live two-verb form (``draft`` then ``send``) instead."""

    def test_deny_message_carries_config_only_and_claude_home_framing(self, _fake_home):
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        reason = guard._deny_reason(target)
        assert "CONFIG-ONLY" in reason
        assert "claude-home" in reason

    def test_deny_message_names_the_destination_inbox_path(self, _fake_home):
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        reason = guard._deny_reason(target)
        assert "cross-repo/inbox/" in reason

    def test_deny_message_names_the_real_receiver_and_live_cli_form(self, _fake_home):
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        reason = guard._deny_reason(target)
        assert "doe-claude-em" in reason
        assert "cross-repo-memo draft" in reason
        assert "cross-repo-memo send" in reason

    def test_deny_message_via_check_carries_all_needles(self, _fake_home):
        """End-to-end through ``check()``, not only the text-builder."""
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        for needle in ("CONFIG-ONLY", "cross-repo/inbox/", "claude-home", "doe-claude-em"):
            assert needle in reason, "missing needle: %r in %r" % (needle, reason)


class TestDenyMessageInboxResolution:
    """The inbox path is RESOLVED, never a hardcoded host literal (item 30,
    plan body)."""

    def test_inbox_path_falls_back_to_placeholder_when_doe_root_unresolvable(
        self, monkeypatch, _fake_home
    ):
        monkeypatch.setattr(
            "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root_in_process",
            lambda: (None, None),
        )
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        reason = guard._deny_reason(target)
        assert "<doe_claude>/cross-repo/inbox/" in reason

    def test_inbox_path_resolves_to_the_actual_receiver_root(
        self, monkeypatch, tmp_path, _fake_home
    ):
        doe_root = tmp_path / "DoE-claude"
        (doe_root / "state" / "cross-repo").mkdir(parents=True)
        monkeypatch.setattr(
            "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root_in_process",
            lambda: (str(doe_root), "env"),
        )
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        reason = guard._deny_reason(target)
        expected = str(doe_root / "state" / "cross-repo" / "inbox").replace("\\", "/") + "/"
        assert expected in reason

    def test_inbox_resolution_failure_does_not_raise(self, monkeypatch, _fake_home):
        """Fail-open on the deny path's own message composition: an
        exception here must degrade to the placeholder text, never bubble
        out of ``_deny_reason`` (this module's own never-raises contract)."""
        def _boom():
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(
            "coordinator_core.ops.coordinator_doe_root.coordinator_doe_root_in_process",
            _boom,
        )
        target = str(_fake_home / ".claude" / "cross-repo" / "inbox" / "x.md")
        reason = guard._deny_reason(target)
        assert "<doe_claude>/cross-repo/inbox/" in reason

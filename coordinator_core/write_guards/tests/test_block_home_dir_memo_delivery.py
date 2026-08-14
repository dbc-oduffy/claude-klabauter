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
        r"""2026-08-03 residual close (PM-authorized) --
        `state/audits/2026-08-03-extended-length-prefix-call-site-audit.md`:
        this guard pre-processes candidate/roots with `casefold_path`
        (already prefix-stripping) and hands the STRINGS to
        `contained_path`, which does its OWN internal `.resolve()` --
        downstream of that string-level fix. Simulates the exact
        length-triggered asymmetry a real Windows host produces: the
        candidate's internal resolve grows the `\\?\` prefix, the guarded
        root's does not. Pre-fix, `relative_to()` compared the prefixed
        candidate `Path` against the bare root `Path`, raised `ValueError`
        for every root, and this guard silently ALLOWED a hand-written
        write straight into `~/.claude/cross-repo/inbox/` -- the exact
        write this guard exists to deny. Red assertion pre-fix:
        `assert result is not None` -> AssertionError (result was None)."""
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

    def test_case_varied_target_denied(self, _fake_home):
        # macOS/APFS is case-insensitive-but-case-preserving: a Write to
        # Cross-Repo/Inbox lands inside the same real guarded cross-repo/
        # directory on disk. os.path.normcase is a no-op on POSIX, so this
        # guard must casefold explicitly (mirrors
        # block_oss_mirror_memo_delivery's own case test).
        # (Review: code-reviewer -- case bypass in this guard, 2026-07-31.)
        target = str(_fake_home / ".Claude" / "Cross-Repo" / "Inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

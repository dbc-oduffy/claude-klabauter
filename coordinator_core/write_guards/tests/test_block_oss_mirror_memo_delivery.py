
from __future__ import annotations

import os
import pathlib

import pytest

from coordinator_core.write_guards import block_oss_mirror_memo_delivery as guard


def _payload(file_path: str, tool_name: str = "Write") -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    }


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(guard.OVERRIDE_ENV, raising=False)


@pytest.fixture
def _mirror(tmp_path):
    mirror_root = tmp_path / "coordinator-claude"
    (mirror_root / "cross-repo" / "inbox").mkdir(parents=True)
    (mirror_root / "coordinator" / "skills" / "foo").mkdir(parents=True)
    return mirror_root


@pytest.fixture
def _mock_mirrors(monkeypatch, _mirror):
    def _fake_read_publish_mirrors():
        return {
            "coordinator_claude": {
                "owner": "claude-central-em",
                "path": str(_mirror),
                "aliases": [],
            }
        }

    monkeypatch.setattr(guard, "read_publish_mirrors", _fake_read_publish_mirrors)
    return _mirror


@pytest.fixture
def _sibling_repo(tmp_path):
    repo = tmp_path / "some-sibling-repo"
    (repo / "cross-repo" / "inbox").mkdir(parents=True)
    return repo


class TestDenyAndAllow:
    def test_hand_written_inbox_write_denied(self, _mock_mirrors):
        target = str(_mock_mirrors / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_nested_cross_repo_path_denied(self, _mock_mirrors):
        target = str(_mock_mirrors / "cross-repo" / "archive" / "2026-07" / "x.md")
        result = guard.check(_payload(target))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_override_env_allows(self, monkeypatch, _mock_mirrors):
        monkeypatch.setenv(guard.OVERRIDE_ENV, "1")
        target = str(_mock_mirrors / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is None

    def test_override_env_never_advertised_in_deny_text(self, _mock_mirrors):
        """B6 (docs/wiki/guard-messaging.md § Register) bans naming any
        override artifact -- env-var key, assignment form, sentinel, doc
        pointer -- in a denial to an audience that isn't a positively
        resolved EM; this guard's `_payload()` builds no `session_id`/
        `agent_id`, so the audience is unresolved and must degrade to
        silence (`operator_override_note`'s 2026-08-13 audience-gated
        reshape, NEGATIVE SPEC 6). This test previously asserted the
        opposite (that the bare env-var name WAS present) -- that predates
        the reshape and pinned a doctrine violation as a spec."""
        target = str(_mock_mirrors / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert guard.OVERRIDE_ENV not in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_non_guarded_tool_allowed(self, _mock_mirrors):
        target = str(_mock_mirrors / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target, tool_name="Read"))
        assert result is None

    def test_state_nested_cross_repo_write_denied(self, _mock_mirrors):
        target = str(_mock_mirrors / "state" / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_case_varied_target_denied(self, _mock_mirrors):
        target = str(_mock_mirrors / "Cross-Repo" / "Inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestPercolationRegression:

    def test_ordinary_percolation_write_in_mirror_is_allowed(self, _mock_mirrors):
        target = str(_mock_mirrors / "coordinator" / "skills" / "foo" / "SKILL.md")
        result = guard.check(_payload(target))
        assert result is None

    def test_mirror_root_itself_is_allowed(self, _mock_mirrors):
        target = str(_mock_mirrors / "README.md")
        result = guard.check(_payload(target))
        assert result is None


class TestUnrelatedPaths:
    def test_legitimate_memo_into_real_sibling_inbox_allowed(
        self, _mock_mirrors, _sibling_repo
    ):
        target = str(_sibling_repo / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is None

    def test_string_containing_cross_repo_outside_mirror_allowed(
        self, _mock_mirrors, tmp_path
    ):
        target = str(tmp_path / "some-project" / "cross-repo-notes" / "x.md")
        result = guard.check(_payload(target))
        assert result is None

    def test_ordinary_write_elsewhere_is_allowed(self, _mock_mirrors, tmp_path):
        target = str(tmp_path / "unrelated" / "file.py")
        result = guard.check(_payload(target))
        assert result is None


class TestHardwareGatedBoundary:
    @pytest.mark.skipif(
        os.name == "nt",
        reason=(
            "Asserts a POSIX-interpreter property: constructing a concrete "
            "WindowsPath raises NotImplementedError only on non-Windows. On "
            "Windows -- this repo's first-class platform -- it constructs "
            "fine, so the test is unsatisfiable here rather than failing to "
            "detect anything. The claim it pins (that .resolve() Windows-path "
            "logic is genuinely hardware-gated, not merely unauthored) is a "
            "statement ABOUT POSIX runs and is only meaningful on one."
        ),
    )
    def test_windows_path_resolve_is_hardware_gated(self, monkeypatch):
        monkeypatch.setattr(os, "name", "nt")
        windows_shaped = "C:" + "\\Users\\" + "dev" + "\\coordinator-claude\\cross-repo\\inbox\\x.md"
        with pytest.raises(NotImplementedError):
            pathlib.Path(windows_shaped).resolve()


class TestUnresolvableRegistry:
    def test_no_mirrors_declared_allows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(guard, "read_publish_mirrors", lambda: {})
        target = str(tmp_path / "coordinator-claude" / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is None

    def test_mirror_with_no_path_allows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            guard,
            "read_publish_mirrors",
            lambda: {"coordinator_claude": {"owner": "claude-central-em", "path": None, "aliases": []}},
        )
        target = str(tmp_path / "coordinator-claude" / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is None

    def test_registry_read_raises_allows(self, monkeypatch, tmp_path):
        def _raise():
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(guard, "read_publish_mirrors", _raise)
        target = str(tmp_path / "coordinator-claude" / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is None

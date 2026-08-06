"""Behavioral tests for
coordinator_core.write_guards.block_oss_mirror_memo_delivery.

Closes the direct-``Write`` leg of the OSS-mirror memo hole — see the
guard's own module docstring for the full contract. This file covers:
the deny path (hand-written delivery into a resolved mirror's
``cross-repo/**``), the primary regression risk (an ordinary percolation
write elsewhere under the SAME mirror must stay silent), a legitimate memo
into a real sibling repo's inbox, a bare string match outside any mirror
root, unresolvable-registry fail-open, and the override env var.

Windows-separator/case-varied forms: this guard's containment check
(`contained_path`, via `Path.resolve()`) is the SAME hardware-gated
primitive `block_home_dir_memo_delivery` and `test_windows_platform_
simulation.py` already document as unreachable from a POSIX interpreter —
`pathlib.Path.__new__` dispatches to `WindowsPath` only when `os.name ==
'nt'`, and instantiating a concrete `WindowsPath` off-Windows raises
`NotImplementedError` at construction time, before `.resolve()` ever runs.
`TestHardwareGatedBoundary` below pins that as an executable fact (mirroring
the precedent test) rather than attempting a genuine Windows-shaped
end-to-end deny assertion on this host; real Windows verification is
deferred the same way it is for the guard this one is modelled on.
"""

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
    """A fake OSS-mirror clone with a cross-repo/inbox/ directory, registered
    as the sole publish mirror this test's mocked `read_publish_mirrors`
    resolves."""
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
    """A distinct, ordinary (non-mirror) sibling repo with its own real inbox."""
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

    def test_override_env_advertised_in_deny_text(self, _mock_mirrors):
        target = str(_mock_mirrors / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target))
        assert guard.OVERRIDE_ENV in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_non_guarded_tool_allowed(self, _mock_mirrors):
        target = str(_mock_mirrors / "cross-repo" / "inbox" / "x.md")
        result = guard.check(_payload(target, tool_name="Read"))
        assert result is None

    def test_case_varied_target_denied(self, _mock_mirrors):
        # macOS/APFS is case-insensitive-but-case-preserving: a Write to
        # Cross-Repo/Inbox lands inside the same real guarded cross-repo/
        # directory on disk. os.path.normcase is a no-op on POSIX, so this
        # guard must casefold explicitly (mirrors
        # block_derived_global_doctrine_write's test_case_varied_denied).
        # (Review: code-reviewer -- Finding 3, 2026-07-31.)
        target = str(_mock_mirrors / "Cross-Repo" / "Inbox" / "x.md")
        result = guard.check(_payload(target))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestPercolationRegression:
    """The primary regression risk named by the brief: an ordinary write
    elsewhere in the SAME mirror (percolation's whole purpose) must be
    silent."""

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
    def test_windows_path_resolve_is_hardware_gated(self, monkeypatch):
        """Proves (rather than merely asserts) that a genuine Windows-shaped
        path's `.resolve()` -- the primitive `contained_path` calls -- is
        unreachable from this POSIX interpreter, per the module docstring
        above and `test_windows_platform_simulation.py`'s identical pin."""
        monkeypatch.setattr(os, "name", "nt")
        windows_shaped = "C:" + "\\Users\\" + "dev" + "\\coordinator-claude\\cross-repo\\inbox\\x.md"  # abs-path-ok: synthetic literal proving a hardware gate, not a real machine path
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

"""Tests for `_uninstall_remove_navi_role` — the full-remove leg that reverses
DoE's user-level Navi role file at `<claude_home>/.claude/agents/navi.md`.

Zero spawns: every test drives the helper (or the leg's dispatch to it) with a
tmp_path home and a stubbed `resolve_coordinator_root`. The one test that runs
`uninstall_remove_substrate` end-to-end stubs the registry seam rather than
letting it reach a real `machine-local` CLI.

Spec backlink: DoE-claude docs/plans/2026-09-02-navi-installable-user-level-nudge-role.md § C5
Commitment: state/cross-repo-commitments/2026-09-02-claude-klabauter-to-land-the-navi-uninstall-leg.yaml
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coordinator_core.install import uninstall_legs


_TEMPLATE_BODY = b"---\nname: navi\n---\n\ncoordinator:navi-role:v1\n"


def _place(home: Path, body: bytes = _TEMPLATE_BODY) -> Path:
    role_file = home / ".claude" / "agents" / "navi.md"
    role_file.parent.mkdir(parents=True, exist_ok=True)
    role_file.write_bytes(body)
    return role_file


def _shipped_template(tmp_path: Path, body: bytes = _TEMPLATE_BODY) -> Path:
    template = tmp_path / "plugin" / "templates" / "agents" / "navi.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_bytes(body)
    return template


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    """Stub `resolve_coordinator_root` to a plugin tree carrying the role
    template — the leg resolves it there, exactly as
    `_uninstall_purge_operator_config` resolves `templates/CLAUDE.local.md.tmpl`."""
    template = _shipped_template(tmp_path)
    monkeypatch.setattr(
        uninstall_legs, "resolve_coordinator_root", lambda *a, **k: str(tmp_path / "plugin")
    )
    return template


def test_absent_role_file_is_a_noop(tmp_path, shipped):
    errors: list[str] = []
    uninstall_legs._uninstall_remove_navi_role(str(tmp_path), False, errors)
    assert errors == []


def test_pristine_role_file_is_removed(tmp_path, shipped):
    role_file = _place(tmp_path)
    errors: list[str] = []

    uninstall_legs._uninstall_remove_navi_role(str(tmp_path), False, errors)

    assert not role_file.exists()
    assert errors == []


def test_emptied_agents_dir_is_pruned_but_a_shared_one_survives(tmp_path, shipped):
    role_file = _place(tmp_path)
    uninstall_legs._uninstall_remove_navi_role(str(tmp_path), False, [])
    assert not role_file.parent.exists()

    role_file = _place(tmp_path)
    neighbour = role_file.parent / "someone-elses-role.md"
    neighbour.write_bytes(b"not ours\n")

    uninstall_legs._uninstall_remove_navi_role(str(tmp_path), False, [])

    assert not role_file.exists()
    assert neighbour.exists(), "a non-coordinator role must not be swept with the dir"


def test_hand_edited_role_file_is_reported_and_left(tmp_path, shipped, capsys):
    role_file = _place(tmp_path, _TEMPLATE_BODY + b"\noperator's own addition\n")
    errors: list[str] = []

    uninstall_legs._uninstall_remove_navi_role(str(tmp_path), False, errors)

    assert role_file.exists()
    assert errors == [], "report-and-leave is an OUTCOME, never a leg failure"
    stderr = capsys.readouterr().err
    assert str(role_file) in stderr
    assert "hand-edited" in stderr


# Review: overengineering-reviewer(Kira) finding 2 — both setups land on the
# same `template is None or not template.is_file()` branch with identical
# assertions; parametrized rather than kept as two near-duplicate tests.
@pytest.mark.parametrize(
    "setup",
    [
        pytest.param("unresolvable", id="unresolvable-root"),
        pytest.param("missing-file", id="root-resolves-asset-missing"),
    ],
)
def test_template_unavailable_reports_and_leaves(tmp_path, monkeypatch, capsys, setup):
    """Whether the root itself fails to resolve, or resolves but the asset
    is not in it (the pre-C1 state, and the state of any install whose
    plugin tree predates the role), the leg reports and leaves."""
    role_file = _place(tmp_path)

    if setup == "unresolvable":
        def _unresolvable(*_a, **_k):
            raise RuntimeError("no coordinator root on this machine")

        monkeypatch.setattr(uninstall_legs, "resolve_coordinator_root", _unresolvable)
    else:
        (tmp_path / "plugin").mkdir()
        monkeypatch.setattr(
            uninstall_legs, "resolve_coordinator_root", lambda *a, **k: str(tmp_path / "plugin")
        )

    errors: list[str] = []

    uninstall_legs._uninstall_remove_navi_role(str(tmp_path), False, errors)

    assert role_file.exists(), "an unverifiable compare must never delete"
    assert errors == []
    assert "could not be resolved" in capsys.readouterr().err


def test_force_removes_a_hand_edited_role_file(tmp_path, shipped):
    role_file = _place(tmp_path, b"entirely rewritten by the operator\n")
    errors: list[str] = []

    uninstall_legs._uninstall_remove_navi_role(str(tmp_path), True, errors)

    assert not role_file.exists()
    assert errors == []


def test_force_does_not_need_a_resolvable_template(tmp_path, monkeypatch):
    """--force must not depend on the plugin tree still being on disk.

    # Review: overengineering-reviewer(Kira) finding 2 — the prior version
    # monkeypatched resolve_coordinator_root to raise, but `if not force`
    # short-circuits before resolution is ever reached, so the raise could
    # never fire; this asserted nothing beyond test_force_removes_a_hand_
    # edited_role_file. Pinning the real intent directly: force must not
    # even call resolve_coordinator_root, so a future refactor that hoists
    # resolution above the force check fails here.
    """
    role_file = _place(tmp_path, b"rewritten\n")

    calls: list[tuple] = []

    def _spy(*a, **k):
        calls.append((a, k))
        raise RuntimeError("gone")

    monkeypatch.setattr(uninstall_legs, "resolve_coordinator_root", _spy)

    uninstall_legs._uninstall_remove_navi_role(str(tmp_path), True, [])

    assert not role_file.exists()
    assert calls == [], "force must not resolve the template at all"


def test_unlink_failure_is_a_leg_error_not_a_policy_outcome(tmp_path, shipped, monkeypatch):
    """An OSError on the unlink is a FAILURE and appends; report-and-leave does not.
    The two must not collapse into one another."""
    _place(tmp_path)

    def _boom(self):
        raise OSError("device busy")

    monkeypatch.setattr(Path, "unlink", _boom)
    errors: list[str] = []

    uninstall_legs._uninstall_remove_navi_role(str(tmp_path), False, errors)

    assert len(errors) == 1
    assert "device busy" in errors[0]


@pytest.mark.parametrize(
    "mode,expect_called",
    [("full-remove", True), ("revert-to-marketplace", False)],
)
def test_substrate_leg_dispatches_only_on_full_remove(
    tmp_path, monkeypatch, mode, expect_called
):
    """The call site, not just the helper: a future edit that drops the
    dispatch, or moves it out of the full-remove branch, fails here."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    # Registry seam: no CLI, no spawn, nothing to clear.
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(uninstall_legs, "ml_set", lambda *a, **k: True)

    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        uninstall_legs,
        "_uninstall_remove_navi_role",
        lambda claude_home, force, errors: calls.append((claude_home, force)),
    )

    uninstall_legs.uninstall_remove_substrate(mode)

    assert bool(calls) is expect_called
    if expect_called:
        assert calls[0][0] == str(tmp_path)

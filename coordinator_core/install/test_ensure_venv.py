"""
coordinator_core.install.test_ensure_venv — resolution-journal wiring
coverage for coordinator_core.install.ensure_venv (C7 of
docs/research/2026-08-06-install-receipt-persistence-design.md).

This module has no pre-existing test file — `ensure_coordinator_venv`'s
subprocess-heavy mechanics (`_venv_healthy`, `_create_venv`, `_install_deps`,
`_resolve_base_python`, `_resolve_ml_cli`) are each independently
monkeypatchable per the module's own "Native test seam" docstring, so these
tests exercise only the journaling wiring around those seams, not a real
venv build.

`COORDINATOR_PLUGIN_ROOT_TRUSTED=1` bypasses `coordinator_trusted_root_guard`
(fail-loud on an untrusted root) so an arbitrary tmp_path plugin_root can be
used without registering it as a real coordinator/DoE/claude-klabauter anchor.
"""
from __future__ import annotations

import pytest

from coordinator_core.install import ensure_venv as mod
from coordinator_core.install.ensure_venv import EnsureVenvError, ensure_coordinator_venv


@pytest.fixture(autouse=True)
def _trust_any_root(monkeypatch):
    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")


@pytest.fixture
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as journal_mod

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_mod


def _resolved(journal_mod):
    journal = journal_mod.read_journal()
    return journal.get("ensure-venv", {}).get(mod._VENV_TREE_CLAUSE_INDEX)


def test_journal_records_already_healthy_venv(tmp_path, _journal_env, monkeypatch):
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    monkeypatch.setattr(mod, "_venv_healthy", lambda *a, **k: True)
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_set_pin", lambda *a, **k: None)

    status = ensure_coordinator_venv(plugin_root, settings_home_path)

    assert status == "ready"
    resolution = _resolved(_journal_env)
    assert resolution is not None
    venv_dir = settings_home_path / ".coordinator-venv"
    assert [e.path for e in resolution.entries] == [str(venv_dir)]
    assert resolution.entries[0].kind == "file-path"


def test_journal_records_successful_rebuild(tmp_path, _journal_env, monkeypatch):
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    monkeypatch.setattr(mod, "_venv_healthy", lambda *a, **k: False)
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_set_pin", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_base_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(mod, "_create_venv", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_whoami_pkg", lambda *a, **k: plugin_root / "whoami")
    monkeypatch.setattr(mod, "_install_deps", lambda *a, **k: None)

    status = ensure_coordinator_venv(plugin_root, settings_home_path)

    assert status == "rebuilt"
    resolution = _resolved(_journal_env)
    assert resolution is not None
    venv_dir = settings_home_path / ".coordinator-venv"
    assert [e.path for e in resolution.entries] == [str(venv_dir)]


def test_journal_empty_entries_on_failed_rebuild(tmp_path, _journal_env, monkeypatch):
    """A rebuild that fails removes the (partial) venv tree — the tree
    genuinely resolved to nothing this run, distinct from never having
    reached this clause at all (see `ensure_coordinator_venv`'s own comment
    at the `except EnsureVenvError` site)."""
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    monkeypatch.setattr(mod, "_venv_healthy", lambda *a, **k: False)
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_base_python", lambda: "/usr/bin/python3")

    def _boom(*a, **k):
        raise EnsureVenvError("simulated pip failure")

    monkeypatch.setattr(mod, "_create_venv", _boom)

    with pytest.raises(EnsureVenvError):
        ensure_coordinator_venv(plugin_root, settings_home_path)

    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_unreported_on_check_only(tmp_path, _journal_env, monkeypatch):
    """check_only never reaches a mutation decision at all for this
    clause — genuinely "never got there", distinct from the empty-tuple
    "resolved to nothing" case above."""
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    monkeypatch.setattr(mod, "_venv_healthy", lambda *a, **k: False)
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)

    status = ensure_coordinator_venv(plugin_root, settings_home_path, check_only=True)

    assert status == "would-rebuild"
    assert _resolved(_journal_env) is None


def test_journal_omits_entry_when_mutation_disabled(tmp_path, _journal_env, monkeypatch):
    """`ensure_venv.py` itself does not gate its own venv mutation on
    `COORDINATOR_DISABLE_MACHINE_MUTATION` (unlike e.g. shell_rc_guard.py) —
    the journal's OWN append does, via `resolution_journal.record_resolution`'s
    `_refuse_machine_mutation` guard. Setting the kill switch refuses only
    the journal row, leaving this clause UNREPORTED for this run rather than
    journaled with a phantom (or accurate-but-untracked) entry."""
    plugin_root = tmp_path / "plugin-root"
    settings_home_path = tmp_path / "settings-home"
    plugin_root.mkdir()
    settings_home_path.mkdir()

    monkeypatch.setattr(mod, "_venv_healthy", lambda *a, **k: True)
    monkeypatch.setattr(mod, "_resolve_ml_cli", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_set_pin", lambda *a, **k: None)

    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    status = ensure_coordinator_venv(plugin_root, settings_home_path)

    assert status == "ready"
    assert _resolved(_journal_env) is None

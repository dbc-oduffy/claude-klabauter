"""
Production callers of the engine-root class API handle ``(None, 'unresolved')``.

With a tmp_path settings-home whose registry holds no ``claude_klabauter`` and no
stamped ``COORDINATOR_ENGINE_ROOT``, the class API returns ``(None, 'unresolved')``;
engine-invoking entrypoints print the fetch-claude-klabauter message and exit non-zero,
hook and op callers degrade without raising. With a stamped published engine
registered, the same callers resolve it.
"""
from __future__ import annotations

import pytest

from coordinator_core import engine_root

_FETCH_URL = "https://github.com/dbc-oduffy/claude-klabauter"


def _reset() -> None:
    engine_root._reset_shim_cache()
    engine_root._reset_gate_memo()


@pytest.fixture
def no_engine(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    (settings_home / "machine-local").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    for var in (
        "MACHINE_LOCAL_REGISTRY_DIR",
        "CLAUDE_KLABAUTER_ROOT",
        "COORDINATOR_ENGINE_ROOT",
        "CLAUDE_PROJECT_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    _reset()
    shim = engine_root._load_shim()
    monkeypatch.setattr(shim, "_self_located_root", lambda: None)
    yield tmp_path
    _reset()


@pytest.fixture
def published_engine(tmp_path, monkeypatch, no_engine):
    mirror = tmp_path / "claude-klabauter"
    (mirror / "coordinator_core").mkdir(parents=True)
    (mirror / "coordinator_core" / "_engine_stamp").write_text("sha:test\n", encoding="utf-8")
    registry = tmp_path / "settings-home" / "machine-local" / "registry.local.toml"
    registry.write_text(f"\"repos.claude_klabauter\" = '{mirror.as_posix()}'\n", encoding="utf-8")
    _reset()
    return mirror


def test_class_api_returns_unresolved_without_raising(no_engine):
    assert engine_root.coordinator_engine_root_with_class() == (None, "unresolved")


def test_install_entry_prints_fetch_message_and_exits_nonzero(no_engine, capsys):
    from coordinator_core.install import coordinator_install_entry

    assert coordinator_install_entry.main([]) == 1
    assert _FETCH_URL in capsys.readouterr().err


def test_verify_templates_setup_sync_exits_nonzero_with_fetch_message(no_engine, monkeypatch, capsys):
    from coordinator_core.ops import verify_templates_setup_sync as mod

    monkeypatch.setattr(mod, "_resolve_plugin_root", lambda: no_engine)
    assert mod.main([]) == 1
    assert _FETCH_URL in capsys.readouterr().err


def test_state_root_engine_subject_raises_state_root_error_naming_fetch(no_engine):
    from coordinator_core import state_root

    with pytest.raises(state_root.StateRootError) as exc:
        state_root._claude_klabauter_state()
    assert _FETCH_URL in str(exc.value)


def test_install_helpers_raise_runtime_error_naming_fetch(no_engine):
    from coordinator_core.install import first_run, substrate

    for helper in (first_run._engine_root_or_raise, substrate._engine_root_or_raise):
        with pytest.raises(RuntimeError) as exc:
            helper()
        assert _FETCH_URL in str(exc.value)


def test_engine_owned_rows_resolve_degrades_to_empty_root(no_engine):
    from coordinator_core.ops.dispatch_emit import engine_owned_rows

    assert engine_owned_rows.resolve_engine_root() == ("", "unresolved")


def test_forwarder_self_heal_swallows_into_failure_ledger(no_engine):
    from coordinator_core.install import forwarder_self_heal

    forwarder_self_heal.self_heal_forwarders()


def test_hooks_degrade_without_raising(no_engine):
    from coordinator_core.hooks import nudge_cross_repo_cwd_boundary as nudge
    from coordinator_core.hooks import session_start_register_published_engine as reg

    assert nudge._handler({"old_cwd": "/a", "new_cwd": "/b"}) == {}
    assert reg._handler({}) == {}


def test_stamped_published_engine_still_resolves(published_engine):
    root, klass = engine_root.coordinator_engine_root_with_class()
    assert root == published_engine.as_posix() or root == str(published_engine)
    assert klass == "resolved-engine"


def test_engine_owned_rows_resolve_returns_published_engine(published_engine):
    from coordinator_core.ops.dispatch_emit import engine_owned_rows

    root, klass = engine_owned_rows.resolve_engine_root()
    assert root and klass != "unresolved"

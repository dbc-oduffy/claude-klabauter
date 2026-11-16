"""scripts/setup.py wiring for the periodic pruner job: install step and uninstall leg."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from coordinator_core.install import install_root_scope, pruner_scheduler

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import setup as setup_mod  # noqa: E402


@pytest.fixture
def registered(monkeypatch):
    calls = []
    monkeypatch.setattr(pruner_scheduler, "register_pruner_task", lambda root, *a, **k: calls.append(root) or True)
    monkeypatch.setattr(install_root_scope, "redirected_install_reason", lambda: None)
    monkeypatch.setattr(install_root_scope, "unregistered_clone_reason", lambda root: None)
    monkeypatch.setattr(setup_mod, "_note_change", lambda *a, **k: calls.append(("note", a[0])))
    return calls


def test_install_registers_pruner_and_notes_change(registered, tmp_path):
    setup_mod.install_pruner_task(tmp_path, REPO)
    assert registered == [tmp_path, ("note", "pruner")]


def test_install_skips_unregistered_clone(registered, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(install_root_scope, "unregistered_clone_reason", lambda root: "not the registered clone")
    setup_mod.install_pruner_task(tmp_path, REPO)
    assert registered == []
    assert "pruner job not registered" in capsys.readouterr().out


def test_install_failed_registration_notes_nothing(registered, monkeypatch, tmp_path):
    monkeypatch.setattr(pruner_scheduler, "register_pruner_task", lambda root, *a, **k: False)
    setup_mod.install_pruner_task(tmp_path, REPO)
    assert registered == []


def test_main_install_chain_calls_pruner_step_after_sampler():
    src = inspect.getsource(setup_mod.main)
    sampler = src.index("\n        install_host_sampler_task(")
    pruner = src.index("\n        install_pruner_task(")
    assert sampler < pruner < src.index("\n        install_verify_settings_home(")


def test_uninstall_flag_parses():
    assert setup_mod.parse_args(["--i-am-agent", "--uninstall-pruner"]).uninstall_pruner is True
    assert setup_mod.parse_args(["--i-am-agent"]).uninstall_pruner is False
    assert "--uninstall-pruner" in setup_mod.HELP_TEXT


@pytest.mark.parametrize("removed,rc", [(True, 0), (False, 1)])
def test_uninstall_leg_unregisters_and_reports(monkeypatch, removed, rc):
    seen = []
    monkeypatch.setattr(pruner_scheduler, "unregister_pruner_task", lambda: seen.append(1) or removed)
    assert setup_mod.main(["--i-am-agent", "--uninstall-pruner"]) == rc
    assert seen == [1]

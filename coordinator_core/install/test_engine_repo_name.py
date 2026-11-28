from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).with_name("engine_repo_name.py")
_spec = importlib.util.spec_from_file_location("engine_repo_name_under_test", _PATH)
erm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(erm)


def test_names_order_new_first():
    assert erm.ENGINE_REPO_NAMES == (erm.ENGINE_REPO_NAME, erm.LEGACY_ENGINE_REPO_NAME)
    assert erm.ENGINE_REPO_NAME == "agent-klabauter"
    assert erm.LEGACY_ENGINE_REPO_NAME == "claude-klabauter"


def test_pointer_file_names_derive_from_constants_new_first():
    assert erm.pointer_file_names() == (
        f".{erm.ENGINE_REPO_NAME}-root",
        f".{erm.LEGACY_ENGINE_REPO_NAME}-root",
    )


def test_registry_keys_new_first():
    assert erm.registry_keys() == ("repos.agent_klabauter", "repos.claude_klabauter")


def test_upstream_url_defaults_to_new_name_and_accepts_legacy():
    assert erm.upstream_url().endswith("/" + erm.ENGINE_REPO_NAME)
    assert erm.upstream_url(erm.LEGACY_ENGINE_REPO_NAME).endswith("/claude-klabauter")


def test_renaming_the_constants_moves_every_derived_value(monkeypatch):
    monkeypatch.setattr(erm, "ENGINE_REPO_NAME", "x-y")
    monkeypatch.setattr(erm, "ENGINE_REPO_NAMES", ("x-y", erm.LEGACY_ENGINE_REPO_NAME))
    assert erm.pointer_file_names()[0] == ".x-y-root"
    assert erm.registry_keys()[0] == "repos.x_y"
    assert erm.upstream_url().endswith("/x-y")

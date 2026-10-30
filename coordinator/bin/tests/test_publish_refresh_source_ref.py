"""dest-refresh checks a peer publish against the round's pin for the source root, never claude-klabauter's commit for another repo."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parents[1]


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_refresh_source_ref_under_test", _BIN_DIR / "publish.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

CLAUDE_KLABAUTER = str(Path("/repos/claude-klabauter"))
DOE = str(Path("/repos/doe"))


@pytest.fixture
def toplevels(monkeypatch):
    monkeypatch.setattr(publish, "_resolve_show_toplevel", lambda root: root)
    monkeypatch.setattr(publish, "_engine_toplevel_key", lambda: CLAUDE_KLABAUTER)
    monkeypatch.setattr(publish, "_source_ref_override", "m" * 40)


def test_a_foreign_source_root_gets_its_own_round_pin(toplevels):
    assert publish._refresh_source_ref(Path(DOE), {DOE: "d" * 40, CLAUDE_KLABAUTER: "m" * 40}) == "d" * 40


def test_the_claude_klabauter_root_gets_the_commit_override(toplevels):
    assert publish._refresh_source_ref(Path(CLAUDE_KLABAUTER), {}) == "m" * 40


def test_an_unpinned_foreign_root_falls_back_to_head_never_the_claude_klabauter_commit(toplevels):
    assert publish._refresh_source_ref(Path(DOE), {}) == "HEAD"

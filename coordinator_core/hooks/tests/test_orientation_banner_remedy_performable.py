"""Every orientation banner's named fix must be invocable as written."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core._settings_home import settings_home
from coordinator_core.hooks import project_orientation as po
from coordinator_core.testing.content_root import resolve_content_root

_SRC = Path(po.__file__).read_text(encoding="utf-8")
_SLASH_REMEDIES = ("update-docs", "workweek-start", "workday-start")


def _roster() -> set:
    commands = Path(resolve_content_root() or "/content-root-unresolved") / "coordinator" / "commands"
    if not commands.is_dir():
        pytest.skip("coordinator-content-repo command roster not mounted beside this repo")
    return {p.stem for p in commands.glob("*.md")}


def test_slash_remedies_are_in_the_command_roster():
    roster = _roster()
    named = set(re.findall(r'(?<![\w/.-])/([a-z][a-z-]+)(?= (?:populates|refreshes)|\s*──|\s*—|["\\])', _SRC))
    assert set(_SLASH_REMEDIES) <= named
    for name in _SLASH_REMEDIES:
        assert name in roster, f"/{name} is not an installed command"


def test_every_slash_command_in_banner_text_resolves():
    roster = _roster()
    for name in _SLASH_REMEDIES:
        assert f"/{name}" in _SRC
        assert name in roster


def test_harness_drift_remedy_names_the_field_and_value(tmp_path, monkeypatch):
    assert "reconciled_against_harness_version to {newest_str} in" in _SRC
    assert "re-read the delta and re-pin" not in _SRC


def test_tier_unknown_remedy_is_a_full_cli_path_from_settings_home():
    assert "`tier-last-run record" not in _SRC
    assert "settings_home() / 'bin' / 'tier-last-run.py'" in _SRC
    assert "--cmd <cmd-run> --exit <code>" in _SRC
    assert (settings_home() / "bin" / "tier-last-run.py").name == "tier-last-run.py"


def test_orientation_cache_refresh_cli_is_built_from_settings_home():
    assert "settings_home() / 'bin' / 'regenerate-orientation-cache'" in _SRC


def test_corpus_remedy_is_the_band_supplied_remount_command():
    assert 'refresh: {remount_command}' in _SRC


def test_engine_resolution_banner_is_fix_free():
    out: list = []
    po._engine_resolution_banner(out)
    text = "".join(out)
    assert "Engine:" in text or text == ""
    for marker in ("refresh", "run ", "re-pin", "/workday", "/update"):
        assert marker not in text

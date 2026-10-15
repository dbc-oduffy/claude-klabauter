from __future__ import annotations

import importlib.util
import os

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_module():
    path = os.path.join(SCRIPT_DIR, "workday-start-handoff-triage.py")
    spec = importlib.util.spec_from_file_location("workday_start_handoff_triage_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_stale_plans_subcommand_is_gone():
    mod = _load_module()
    with pytest.raises(SystemExit):
        mod._build_parser().parse_args(["stale-plans"])
    assert not hasattr(mod, "find_stale_executing_plans")


def test_remaining_subcommands_parse():
    mod = _load_module()
    parser = mod._build_parser()
    for sub in ("ready", "awaiting-gate", "trim-notes"):
        assert parser.parse_args([sub]).subcommand == sub


def test_trim_notes_surfaces_body_and_rotates_to_header(tmp_path):
    mod = _load_module()
    notes = tmp_path / "notes.md"
    notes.write_text("h1\nh2\nh3\nh4\nb1\nb2\n", encoding="utf-8")
    assert mod.trim_orphan_sweep_notes(str(notes)) == "b1\nb2\n"
    assert notes.read_text(encoding="utf-8") == "h1\nh2\nh3\nh4\n"
    assert mod.trim_orphan_sweep_notes(str(notes)) is None

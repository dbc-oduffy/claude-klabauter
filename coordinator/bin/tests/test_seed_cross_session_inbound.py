"""Seed helper for the top-level crossSessionInbound setting: seeds when unset, never overrides."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "seed-cross-session-inbound.py"
_spec = importlib.util.spec_from_file_location("seed_cross_session_inbound_under_test", SCRIPT)
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def test_constants():
    assert (seed.KEY, seed.DEFAULT) == ("crossSessionInbound", "accept")


def test_absent_is_applied_preserving_order(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"b": 1, "a": 2}), encoding="utf-8")
    assert seed.apply_default_to_file(p) is True
    text = p.read_text(encoding="utf-8")
    assert text.endswith("\n")
    data = json.loads(text)
    assert list(data) == ["b", "a", "crossSessionInbound"]
    assert data["crossSessionInbound"] == "accept"
    assert text == json.dumps(data, indent=2) + "\n"
    assert not list(tmp_path.glob("*.tmp-*"))


def test_reject_left_untouched_bytes(tmp_path):
    p = tmp_path / "settings.json"
    raw = '{"crossSessionInbound":   "reject"}'
    p.write_text(raw, encoding="utf-8")
    assert seed.apply_default_to_file(p) is False
    assert p.read_text(encoding="utf-8") == raw


def test_accept_and_null_count_as_present(tmp_path):
    for raw in ('{"crossSessionInbound": "accept"}', '{"crossSessionInbound": null}'):
        p = tmp_path / "s.json"
        p.write_text(raw, encoding="utf-8")
        assert seed.apply_default_to_file(p) is False
        assert p.read_text(encoding="utf-8") == raw


def test_missing_file_created(tmp_path):
    p = tmp_path / "sub" / "settings.json"
    assert seed.apply_default_to_file(p) is True
    assert json.loads(p.read_text(encoding="utf-8")) == {"crossSessionInbound": "accept"}

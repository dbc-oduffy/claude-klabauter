"""Seed helper for the top-level crossSessionInbound setting: seeds when unset, never overrides."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "seed-cross-session-inbound.py"
_spec = importlib.util.spec_from_file_location("seed_cross_session_inbound_under_test", SCRIPT)
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


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


def test_cli_apply_bad_and_nondict_json_exit_2_untouched(tmp_path):
    for raw in ("{not json", "[1, 2]"):
        p = tmp_path / "s.json"
        p.write_text(raw, encoding="utf-8")
        r = _cli("--apply", str(p))
        assert r.returncode == 2 and r.stderr
        assert p.read_text(encoding="utf-8") == raw


def test_cli_apply_output(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{}", encoding="utf-8")
    assert _cli("--apply", str(p)).stdout.strip() == "applied"
    assert _cli("--apply", str(p)).stdout.strip() == "unchanged"


def test_cli_check_never_writes(tmp_path):
    missing = tmp_path / "none.json"
    r = _cli("--check", str(missing))
    assert (r.returncode, r.stdout.strip()) == (0, "absent")
    assert not missing.exists()

    p = tmp_path / "s.json"
    p.write_text("{}", encoding="utf-8")
    assert _cli("--check", str(p)).stdout.strip() == "absent"
    assert p.read_text(encoding="utf-8") == "{}"

    p.write_text('{"crossSessionInbound": "reject"}', encoding="utf-8")
    r = _cli("--check", str(p))
    assert (r.returncode, r.stdout.strip()) == (0, "present=reject")


def test_cli_check_bad_json_exit_2(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{oops", encoding="utf-8")
    r = _cli("--check", str(p))
    assert r.returncode == 2
    assert p.read_text(encoding="utf-8") == "{oops"

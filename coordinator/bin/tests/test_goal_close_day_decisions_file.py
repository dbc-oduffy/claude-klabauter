"""goal-close-day `_parse_args`: --decisions-file wiring, the shape gate on
both channels, and the pinned `--decisions ""` refusal (an empty string is
not JSON; only an ABSENT flag or an empty `{}` means "close nothing")."""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

_BIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "_cli_goal_close_day_decisions_file", os.path.join(_BIN_DIR, "goal-close-day.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


def _exits(argv, capsys):
    with pytest.raises(SystemExit) as info:
        mod._parse_args(argv)
    return info.value.code, capsys.readouterr().err


def test_absent_flag_is_empty_map():
    assert mod._parse_args([]) == {"decisions": {}}


def test_inline_decisions_parsed():
    assert mod._parse_args(["--decisions", '{"g1": "done"}']) == {"decisions": {"g1": "done"}}


def test_decisions_file_is_read(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"g1": "done", "g2": "dropped"}), encoding="utf-8")
    assert mod._parse_args(["--decisions-file", str(path)]) == {
        "decisions": {"g1": "done", "g2": "dropped"}
    }


def test_inline_and_file_are_mutually_exclusive(tmp_path, capsys):
    path = tmp_path / "d.json"
    path.write_text("{}", encoding="utf-8")
    code, err = _exits(["--decisions", "{}", "--decisions-file", str(path)], capsys)
    assert code == 1
    assert "ERROR: --decisions and --decisions-file are mutually exclusive" in err


def test_unreadable_file_exits_1(tmp_path, capsys):
    code, err = _exits(["--decisions-file", str(tmp_path / "absent.json")], capsys)
    assert code == 1
    assert "ERROR: --decisions-file unreadable" in err


def test_malformed_file_exits_1(tmp_path, capsys):
    path = tmp_path / "d.json"
    path.write_text("{not-json", encoding="utf-8")
    code, err = _exits(["--decisions-file", str(path)], capsys)
    assert code == 1
    assert "ERROR: malformed --decisions JSON (from " in err


def test_file_holding_non_object_exits_1(tmp_path, capsys):
    path = tmp_path / "d.json"
    path.write_text("[1]", encoding="utf-8")
    code, err = _exits(["--decisions-file", str(path)], capsys)
    assert code == 1
    assert "ERROR: --decisions must be a JSON object" in err


def test_inline_non_object_exits_1(capsys):
    code, err = _exits(["--decisions", "[1]"], capsys)
    assert code == 1
    assert "ERROR: --decisions must be a JSON object" in err


def test_missing_value_exits_1(capsys):
    code, err = _exits(["--decisions"], capsys)
    assert code == 1
    assert "ERROR: --decisions requires a value" in err


def test_empty_string_decisions_is_rejected_not_an_empty_map(capsys):
    code, err = _exits(["--decisions", ""], capsys)
    assert code == 1
    assert "ERROR: malformed --decisions JSON" in err

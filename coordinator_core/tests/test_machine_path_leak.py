"""Pins for the shared machine-path-leak scan."""

import json

import pytest

from coordinator_core import machine_path_leak as mpl


@pytest.mark.parametrize(
    "value",
    [
        "/home/someone/x",
        "/Users/someone/x",
        "C:\\Users\\someone\\dev",
        "C:/Users/someone/dev",
        "C:\\dev",
        "E:/dev",
    ],
)
def test_machine_absolute_leaf_is_reported(value):
    detail = mpl.leak_detail("settings.json", json.dumps({"env": {"HOME_X": value}}))
    assert detail is not None
    assert "Leaf path : env.HOME_X" in detail
    assert "Value     : " + value in detail


def test_list_leaf_path_and_clean_content():
    detail = mpl.leak_detail("s.json", json.dumps({"a": ["ok", "/home/u/z"]}))
    assert "Leaf path : a[1]" in detail
    assert mpl.leak_detail("s.json", json.dumps({"a": "relative/x", "b": "/usr/bin"})) is None


def test_unparseable_file_reports_error():
    detail = mpl.leak_detail("s.json", "{not json")
    assert detail.startswith("ERROR — failed to parse s.json as JSON")


def test_output_matches_legacy_scan():
    content = json.dumps({"k": "/home/a/b", "l": ["C:\\Users\\q\\z"]})
    legacy_lines = []
    for p, v in mpl._walk_json(json.loads(content)):
        legacy_lines.append((p, v))
    detail = mpl.leak_detail("settings.json", content)
    assert [p for p, _ in legacy_lines] == ["k", "l[0]"]
    for p, v in legacy_lines:
        assert "Leaf path : {}".format(p) in detail and "Value     : {}".format(v) in detail


@pytest.mark.parametrize(
    "path,expected",
    [
        ("settings.json", True),
        (".claude/settings.json", True),
        ("tests/fixtures/settings.json", True),
        ("settings.local.json", False),
        ("settings.json.bak", False),
    ],
)
def test_is_settings_json(path, expected):
    assert mpl.is_settings_json(path) is expected


def test_fixture_suppression_only_parse_error_under_fixtures():
    fx = "coordinator/tests/fixtures/x/settings.json"
    assert mpl.fixture_suppressible(fx, "ERROR — failed to parse") is True
    assert mpl.fixture_suppressible(fx, "VIOLATION: leak") is False
    assert mpl.fixture_suppressible(".claude/settings.json", "ERROR — x") is False

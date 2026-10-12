"""--stage-returns-json and --judge-result resolve through one helper: inline JSON or a file path."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.review_mint import supersede


def test_inline_json_and_file_path_resolve_alike(tmp_path):
    payload = {"prep": {"slice_files": ["a.txt"]}}
    path = tmp_path / "returns.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert supersede._json_arg(json.dumps(payload)) == payload
    assert supersede._json_arg(str(path)) == payload
    assert supersede._json_arg(None) is None


@pytest.mark.parametrize("flag", ["--stage-returns-json", "--judge-result"])
def test_unreadable_path_exits_2_naming_the_flag(tmp_path, capsys, flag):
    rc = supersede.main([
        "--plan", "p.md", "--session-id", "s", "--base", "a", "--head", "b",
        flag, str(tmp_path / "missing.json"), "--repo-root", str(tmp_path),
    ])

    assert rc == 2
    assert f"{flag} unreadable" in capsys.readouterr().err

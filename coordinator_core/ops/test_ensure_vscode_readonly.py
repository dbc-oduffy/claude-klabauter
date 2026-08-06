"""Tests for coordinator_core.ops.ensure_vscode_readonly.

Golden oracle — ported case-for-case (fresh create, merge-preserving strict
JSON, JSONC with backup, idempotent second run, unparseable-preserved,
bad-arg exit 2 x2).

Port of: test-ensure-vscode-readonly.sh (example-doctrine-repo 894d4bc6, 2026-07-22)
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.ensure_vscode_readonly import main

K1 = "**/state/handoff-tracker.md"
K2 = "**/state/doe-handoff-tracker.md"


def _read(p):
    return json.loads(p.read_text(encoding="utf-8"))


def test_fresh_create_no_vscode_dir(tmp_path, capsys):
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    settings = tmp_path / ".vscode" / "settings.json"
    obj = _read(settings)
    assert obj["files.readonlyInclude"][K1] is True
    assert obj["files.readonlyInclude"][K2] is True


def test_merge_into_strict_json_preserves_other_keys(tmp_path):
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    settings = vscode / "settings.json"
    settings.write_text(
        json.dumps({"editor.tabSize": 2, "files.readonlyInclude": {"**/vendor/**": True}}),
        encoding="utf-8",
    )
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    obj = _read(settings)
    assert obj["editor.tabSize"] == 2
    assert obj["files.readonlyInclude"]["**/vendor/**"] is True
    assert obj["files.readonlyInclude"][K1] is True
    assert obj["files.readonlyInclude"][K2] is True


def test_jsonc_adds_guard_backs_up_original(tmp_path):
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    settings = vscode / "settings.json"
    raw = (
        "{\n  // my editor prefs\n  \"editor.tabSize\": 4,\n"
        '  "files.autoSave": "off", /* trailing */\n}\n'
    )
    settings.write_text(raw, encoding="utf-8")
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    obj = _read(settings)
    assert obj["editor.tabSize"] == 4
    assert obj["files.autoSave"] == "off"
    assert obj["files.readonlyInclude"][K1] is True
    assert obj["files.readonlyInclude"][K2] is True
    backup = vscode / "settings.json.bak"
    assert backup.is_file()
    assert "my editor prefs" in backup.read_text(encoding="utf-8")


def test_idempotent_second_run_byte_identical(tmp_path):
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    settings = tmp_path / ".vscode" / "settings.json"
    first = settings.read_text(encoding="utf-8")
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    second = settings.read_text(encoding="utf-8")
    assert first == second


def test_unparseable_settings_preserved_not_clobbered(tmp_path):
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    settings = vscode / "settings.json"
    raw = "{ this is not json at all ::: }"
    settings.write_text(raw, encoding="utf-8")
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    assert settings.read_text(encoding="utf-8") == raw


def test_root_missing_value_exits_2(capsys):
    rc = main(["--root"])
    assert rc == 2


def test_unknown_arg_exits_2(capsys):
    rc = main(["--bogus"])
    assert rc == 2


def test_root_not_a_directory_exits_2(tmp_path):
    not_a_dir = tmp_path / "nope"
    rc = main(["--root", str(not_a_dir)])
    assert rc == 2

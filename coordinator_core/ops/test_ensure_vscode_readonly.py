"""Tests for coordinator_core.ops.ensure_vscode_readonly.

The two `files.readonlyInclude` globs this module once merged protected the
generated handoff-tracker renders, both retired fleet-wide — see
docs/plans/2026-08-14-retire-the-handoff-tracker-and-project-tracker-renders.md.
`GUARD_KEYS` is now empty, so `_merge_settings` is a permanent no-op: it never
writes `settings.json`, regardless of the file's starting shape. These cases
assert that no-op contract (byte-identical / absent) rather than a merge that
no longer happens.

Was: golden-oracle port of test-ensure-vscode-readonly.sh (DoE 894d4bc6,
2026-07-22) — the merge/backup/idempotent assertions below are retired
alongside the guard itself.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.ensure_vscode_readonly import GUARD_KEYS, main


def test_guard_keys_is_empty():
    assert GUARD_KEYS == {}


def test_fresh_create_no_vscode_dir_writes_nothing(tmp_path, capsys):
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    settings = tmp_path / ".vscode" / "settings.json"
    assert not settings.exists()


def test_existing_settings_json_untouched(tmp_path):
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    settings = vscode / "settings.json"
    raw = json.dumps({"editor.tabSize": 2, "files.readonlyInclude": {"**/vendor/**": True}})
    settings.write_text(raw, encoding="utf-8")
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    assert settings.read_text(encoding="utf-8") == raw


def test_jsonc_settings_untouched_no_backup_written(tmp_path):
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
    assert settings.read_text(encoding="utf-8") == raw
    backup = vscode / "settings.json.bak"
    assert not backup.exists()


def test_idempotent_second_run_byte_identical(tmp_path):
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    settings = tmp_path / ".vscode" / "settings.json"
    assert not settings.exists()
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    assert not settings.exists()


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

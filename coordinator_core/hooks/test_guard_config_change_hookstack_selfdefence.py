from __future__ import annotations

import json

from coordinator_core.hooks import guard_config_change_hookstack_selfdefence as m


def test_disable_all_hooks_write_flags_the_sharper_message(tmp_path):
    settings_file = tmp_path / "settings.local.json"
    settings_file.write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")

    result = m._handler({"source": "local_settings", "file_path": str(settings_file)})
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert result["hookSpecificOutput"]["hookEventName"] == "ConfigChange"
    assert "hookstack was just disabled" in ctx


def test_generic_out_of_band_edit_flags_the_generic_message(tmp_path):
    settings_file = tmp_path / "settings.local.json"
    settings_file.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")

    result = m._handler({"source": "local_settings", "file_path": str(settings_file)})
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "edited by a process outside the tool pipeline" in ctx


def test_non_local_settings_source_is_a_no_op():
    assert m._handler({"source": "project_settings", "file_path": "/x"}) == {}

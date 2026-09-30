"""Tests for the stdlib-only hook-plane verdict module, over tmp fixtures only."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from coordinator_core.install import hook_plane_verdict as hpv

TEN_KEYS = {
    "settings_path",
    "hooks_registered",
    "hook_delivery",
    "hook_event_count",
    "settings_read_error",
    "plugin_hooks",
    "content_root_resolves",
    "content_root_rungs",
    "content_root_bin_dir",
    "content_root_bin_resolves",
}
KEY = "coord@market"


def _json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _plugin(tmp_path: Path, hooks_json, *, files=()) -> Path:
    root = tmp_path / "plugin"
    _json(root / ".claude-plugin" / "plugin.json", {"name": "coord"})
    _json(root / ".claude-plugin" / "marketplace.json", {"name": "market"})
    if hooks_json is not None:
        _json(root / "hooks" / "hooks.json", hooks_json)
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("", encoding="utf-8")
    return root


def _record(home: Path, install_path: Path) -> None:
    _json(
        home / "plugins" / "installed_plugins.json",
        {"plugins": {KEY: [{"installPath": str(install_path)}]}},
    )


def _hooks(command: str) -> dict:
    return {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}


def test_ac1_module_imports_only_stdlib():
    tree = ast.parse(Path(hpv.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add((node.module or "").split(".")[0])
    assert names
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)


@pytest.mark.parametrize(
    "settings_hooks,plugin_on,expected",
    [(False, False, "none"), (True, False, "settings"), (False, True, "plugin"), (True, True, "both")],
)
def test_ac4_delivery_combinations_and_ten_keys(tmp_path, settings_hooks, plugin_on, expected):
    home = tmp_path / "home"
    root = _plugin(
        tmp_path,
        _hooks("python3 ${CLAUDE_PLUGIN_ROOT}/hooks/run.py"),
        files=["hooks/run.py"],
    )
    _record(home, root)
    settings: dict = {"enabledPlugins": {KEY: plugin_on}}
    if settings_hooks:
        settings["hooks"] = {"Stop": [{"hooks": []}]}
    _json(home / "settings.json", settings)
    plane = hpv.derive_hook_plane(claude_home=home, plugin_root=root, settings_home=None)
    assert set(plane) == TEN_KEYS
    assert plane["hook_delivery"] == expected
    assert plane["hooks_registered"] is (expected != "none")
    assert "_delivered" not in plane["plugin_hooks"]


def test_content_root_rungs(tmp_path):
    home = tmp_path / "home"
    sh = tmp_path / "sh"
    ml = sh / "machine-local"
    ml.mkdir(parents=True)

    def derive():
        return hpv.derive_hook_plane(claude_home=home, plugin_root=None, settings_home=sh)

    plane = derive()
    assert plane["content_root_resolves"] is False
    assert plane["content_root_bin_dir"] is None

    home.mkdir()
    (home / ".coordinator-content-root").write_text("/legacy\n", encoding="utf-8")
    plane = derive()
    assert plane["content_root_resolves"] is True
    assert plane["content_root_bin_dir"] == str(Path("/legacy") / "coordinator" / "bin")

    (ml / ".coordinator-content-root").write_text("/settings-file\n", encoding="utf-8")
    assert derive()["content_root_bin_dir"] == str(Path("/settings-file") / "coordinator" / "bin")

    (ml / "registry.toml").write_text('"repos.content_root" = \'/registry\'\n', encoding="utf-8")
    plane = derive()
    assert plane["content_root_rungs"]["registry repos.content_root"] == "/registry"
    assert plane["content_root_bin_dir"] == str(Path("/registry") / "coordinator" / "bin")

    (ml / "registry.local.toml").write_text('"repos.content_root" = \'/local\'\n', encoding="utf-8")
    assert derive()["content_root_rungs"]["registry repos.content_root"] == "/local"


def test_no_settings_home_uses_placeholder_rung(tmp_path):
    plane = hpv.derive_hook_plane(claude_home=tmp_path, plugin_root=None, settings_home=None)
    assert "<settings-home>/machine-local/.coordinator-content-root" in plane["content_root_rungs"]
    assert plane["settings_read_error"] is not None


def test_bin_dir_resolves_when_present(tmp_path):
    doe = tmp_path / "doe"
    (doe / "coordinator" / "bin").mkdir(parents=True)
    (tmp_path / ".coordinator-content-root").write_text(str(doe), encoding="utf-8")
    plane = hpv.derive_hook_plane(claude_home=tmp_path, plugin_root=None, settings_home=None)
    assert plane["content_root_bin_resolves"] is True


def test_plugin_root_none_never_raises(tmp_path):
    result = hpv.plugin_hook_delivery({}, claude_home=tmp_path, plugin_root=None)
    assert result["armed"] is False
    assert result["reason"] == "no plugin root resolved"
    plane = hpv.derive_hook_plane(claude_home=tmp_path, plugin_root=None, settings_home=None)
    assert plane["plugin_hooks"]["reason"] == "no plugin root resolved"


def test_manifest_naming_absent_file_is_not_armed(tmp_path):
    home = tmp_path / "home"
    root = _plugin(tmp_path, _hooks("python3 ${CLAUDE_PLUGIN_ROOT}/hooks/gone.py"))
    _record(home, root)
    result = hpv.plugin_hook_delivery(
        {"enabledPlugins": {KEY: True}}, claude_home=home, plugin_root=root
    )
    assert result["armed"] is False
    assert result["missing_files"] == ["hooks/gone.py"]
    assert "absent file" in result["reason"]


def test_quoted_path_with_space_keeps_full_tail(tmp_path):
    home = tmp_path / "home"
    root = _plugin(
        tmp_path,
        _hooks('python3 "${CLAUDE_PLUGIN_ROOT}/my hooks/run one.py"'),
        files=["my hooks/run one.py"],
    )
    _record(home, root)
    result = hpv.plugin_hook_delivery(
        {"enabledPlugins": {KEY: True}}, claude_home=home, plugin_root=root
    )
    assert result["armed"] is True
    assert result["_delivered"] == {"SessionStart": {"my hooks/run one.py"}}
    assert hpv.hook_identities({"command": 'python3 "/x/my hooks/run one.py"'}) == {
        "my hooks/run one.py"
    }


def test_backslash_hook_command_is_normalised():
    assert hpv.script_tail("C:\\plug\\hooks\\run.py") == "hooks/run.py"
    assert hpv.script_tail("notes.txt") is None
    quoted = {"command": 'python3 "${CLAUDE_PLUGIN_ROOT}\\hooks\\run.py"'}
    assert hpv.hook_identities(quoted) == {"hooks/run.py"}


def test_unbalanced_quote_falls_back_to_split():
    assert hpv.shlex_pieces('python3 "a/b.py') == ["python3", '"a/b.py']


def test_http_hook_identity():
    assert hpv.hook_identities({"type": "http", "url": "http://h/x"}) == {"url:http://h/x"}
    assert hpv.hook_identities({"type": "http"}) == set()


def test_plugin_record_key_raises_on_missing_name(tmp_path):
    root = tmp_path / "p"
    _json(root / ".claude-plugin" / "plugin.json", {})
    _json(root / ".claude-plugin" / "marketplace.json", {"name": "m"})
    with pytest.raises(ValueError):
        hpv.plugin_record_key(root)


def test_status_line_and_problems():
    armed = {"hook_delivery": "plugin", "hooks_registered": True, "content_root_resolves": True}
    assert hpv.hook_plane_status_line(armed) == "HOOK PLANE: ARMED (delivery: plugin)"
    assert (
        hpv.hook_plane_status_line(armed, extra_failure="commit hook: MISSING")
        == "HOOK PLANE: UNARMED (delivery: plugin; commit hook: MISSING)"
    )
    assert hpv.hook_plane_status_line(None) == "HOOK PLANE: UNARMED (delivery: unknown)"
    assert hpv.hook_plane_problems(armed) == []
    bad = {
        "hook_delivery": "none",
        "hooks_registered": False,
        "content_root_resolves": False,
        "settings_path": "s.json",
        "settings_read_error": "OSError: x",
        "plugin_hooks": {"reason": "nope"},
    }
    problems = hpv.hook_plane_problems(bad)
    assert len(problems) == 2
    assert "(OSError: x)" in problems[0] and "(nope)" in problems[0]
    assert hpv.hook_plane_status_line(bad).startswith("HOOK PLANE: UNARMED")


def test_write_rule_surface(tmp_path):
    assert hpv.write_rule_surface(tmp_path, "a.md", "x\n") is True
    assert (tmp_path / "rules" / "a.md").read_bytes() == b"x\n"
    assert hpv.write_rule_surface(tmp_path, "a.md", None) is False
    assert not (tmp_path / "rules" / "a.md").exists()


def test_content_root_bin_dir_resolves_on_flat_mirror(tmp_path):
    flat = tmp_path / "flat"
    (flat / ".claude-plugin").mkdir(parents=True)
    (flat / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    (flat / "bin").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".coordinator-content-root").write_text(f"{flat}\n", encoding="utf-8")
    plane = hpv.derive_hook_plane(claude_home=home, plugin_root=None, settings_home=None)
    assert plane["content_root_bin_dir"] == str(flat / "bin")
    assert plane["content_root_bin_resolves"] is True

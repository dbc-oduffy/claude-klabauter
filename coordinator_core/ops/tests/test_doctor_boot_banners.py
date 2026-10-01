"""Doctor boot-banner layers: hook delivery duplication and the hook-plane verdict, over tmp surfaces."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import doctor_boot_banners as banners

_SCRIPT = "hooks/scripts/guard_a.py"


def _hooks_block(command: str) -> dict:
    return {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    root = tmp_path / "doe"
    content = root / "coordinator"
    (content / "hooks" / "scripts").mkdir(parents=True)
    (content / _SCRIPT).write_text("print('x')\n")
    config = tmp_path / "config"
    config.mkdir()
    settings_home = tmp_path / "settings-home"
    (settings_home / "machine-local").mkdir(parents=True)
    (settings_home / "machine-local" / ".coordinator-content-root").write_text(str(root) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(root))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    return {"root": root, "content": content, "config": config}


def _plugin_hooks(env: dict) -> None:
    command = "python3 ${CLAUDE_PLUGIN_ROOT}/" + _SCRIPT
    (env["content"] / "hooks" / "hooks.json").write_text(json.dumps({"hooks": _hooks_block(command)}))


def _settings_hooks(env: dict) -> None:
    command = f"python3 {env['content'] / _SCRIPT}"
    (env["config"] / "settings.json").write_text(json.dumps({"hooks": _hooks_block(command)}))


def test_both_surfaces_delivering_the_same_script_is_broken(env: dict):
    _plugin_hooks(env)
    _settings_hooks(env)
    layer = banners.hook_delivery_layer(env["config"])
    assert layer.status == "broken"
    assert layer.findings and layer.findings[0].severity == "broken"


def test_unresolvable_content_root_with_settings_hooks_is_unknown(
    env: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _settings_hooks(env)
    from coordinator_core.ops.session import guard_settings_integrity as gsi

    def _raise():
        raise gsi.ResolveCoordinatorCloneError("no content root")

    monkeypatch.setattr(gsi, "resolve_content_root", _raise)
    layer = banners.hook_delivery_layer(env["config"])
    assert layer.status == "unknown"


def test_single_surface_is_ok(env: dict):
    _plugin_hooks(env)
    layer = banners.hook_delivery_layer(env["config"])
    assert layer.status == "ok"
    assert all(f.severity != "broken" for f in layer.findings)


def test_no_hooks_anywhere_is_broken_with_the_problem_text(env: dict):
    layer = banners.hook_plane_layer(env["config"])
    assert layer.status == "broken"
    assert any("is empty" in f.message for f in layer.findings)


def test_content_root_resolving_through_no_rung_is_broken(env: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _settings_hooks(env)
    (tmp_path / "settings-home" / "machine-local" / ".coordinator-content-root").unlink()
    layer = banners.hook_plane_layer(env["config"])
    assert layer.status == "broken"
    assert any("`.coordinator-content-root` resolves through no rung" in f.message for f in layer.findings)


def test_armed_plane_is_ok_with_the_status_line(env: dict):
    _settings_hooks(env)
    layer = banners.hook_plane_layer(env["config"])
    assert layer.status == "ok"
    assert layer.findings[0].severity == "info"
    assert layer.findings[0].message.startswith("HOOK PLANE: ARMED")


def test_unresolvable_settings_home_does_not_raise(env: dict, monkeypatch: pytest.MonkeyPatch):
    _settings_hooks(env)
    from coordinator_core import _settings_home

    def _raise():
        raise RuntimeError("no home")

    monkeypatch.setattr(_settings_home, "settings_home", _raise)
    layer = banners.hook_plane_layer(env["config"])
    assert layer.status in {"ok", "broken"}


def test_boot_banner_layers_returns_both_in_order(env: dict):
    layers = banners.boot_banner_layers(env["config"])
    assert [l.name for l in layers] == [banners.HOOK_DELIVERY_NAME, banners.HOOK_PLANE_NAME]


def test_a_raising_reader_yields_unknown_and_still_two_layers(env: dict, monkeypatch: pytest.MonkeyPatch):
    from coordinator_core.ops.session import guard_settings_integrity as gsi

    def _boom(*_a, **_k):
        raise ValueError("reader exploded")

    monkeypatch.setattr(gsi, "detect_hook_delivery_duplication", _boom)
    layers = banners.boot_banner_layers(env["config"])
    assert len(layers) == 2
    assert layers[0].status == "unknown"
    assert "reader exploded" in layers[0].findings[0].message

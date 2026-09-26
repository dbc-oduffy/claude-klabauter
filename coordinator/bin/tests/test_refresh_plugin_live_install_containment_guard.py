from __future__ import annotations

import importlib.util
from pathlib import Path

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "refresh_plugin_live_install",
        _BIN_DIR / "refresh-plugin-live-install.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()
_resolve_contained_live_path = _mod._resolve_contained_live_path


def test_contained_live_path_passes(tmp_path):
    plugins_dir = tmp_path / "plugins"
    live_path = plugins_dir / "my-plugin"
    live_path.mkdir(parents=True)
    source_path = tmp_path / "source-checkout"
    source_path.mkdir()

    result = _resolve_contained_live_path(
        str(live_path), str(source_path), "", plugins_dir, "my-plugin", "git-managed",
    )

    assert result == live_path.resolve(strict=True)


def test_live_path_outside_plugins_dir_is_refused(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    live_path = tmp_path / "elsewhere" / "not-in-plugins"
    live_path.mkdir(parents=True)
    source_path = tmp_path / "source-checkout"
    source_path.mkdir()

    result = _resolve_contained_live_path(
        str(live_path), str(source_path), "", plugins_dir, "my-plugin", "git-managed",
    )

    assert result is None


def test_live_path_equal_source_path_refused_under_git_managed(tmp_path):
    plugins_dir = tmp_path / "plugins"
    shared = plugins_dir / "my-plugin"
    shared.mkdir(parents=True)

    result = _resolve_contained_live_path(
        str(shared), str(shared), "", plugins_dir, "my-plugin", "git-managed",
    )

    assert result is None


def test_live_path_equal_source_path_allowed_under_source_is_live(tmp_path):
    plugins_dir = tmp_path / "plugins"
    shared = plugins_dir / "my-plugin"
    shared.mkdir(parents=True)

    result = _resolve_contained_live_path(
        str(shared), str(shared), "source_is_live", plugins_dir, "my-plugin", "git-managed",
    )

    assert result == shared.resolve(strict=True)


def test_nonexistent_live_path_is_refused(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    live_path = plugins_dir / "does-not-exist"
    source_path = tmp_path / "source-checkout"
    source_path.mkdir()

    result = _resolve_contained_live_path(
        str(live_path), str(source_path), "", plugins_dir, "my-plugin", "git-managed",
    )

    assert result is None


def test_nonexistent_source_path_is_refused_when_not_source_is_live(tmp_path):
    plugins_dir = tmp_path / "plugins"
    live_path = plugins_dir / "my-plugin"
    live_path.mkdir(parents=True)
    source_path = tmp_path / "does-not-exist-source"

    result = _resolve_contained_live_path(
        str(live_path), str(source_path), "copy_install", plugins_dir, "my-plugin", "copy_install",
    )

    assert result is None


def test_copy_install_leg_uses_same_containment_guard(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    live_path = tmp_path / "elsewhere" / "not-in-plugins"
    live_path.mkdir(parents=True)
    snapshots_dir = tmp_path / "snapshots"
    refresh_log = tmp_path / "refresh-log"
    registry_local = tmp_path / "registry.local.toml"

    rc = _mod._handle_copy_install(
        "my-plugin",
        str(tmp_path / "source-checkout"),
        str(live_path),
        "echo hi",
        False,
        plugins_dir,
        snapshots_dir,
        refresh_log,
        registry_local,
    )

    assert rc == 1

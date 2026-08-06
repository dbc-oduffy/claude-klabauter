"""test_refresh_plugin_live_install_containment_guard — pytest coverage for the
shared live_path containment guard in refresh-plugin-live-install.py.

Spec backlink: git-managed leg (`_handle_default`) previously had NO rm-rf-class
containment guard on its registry-supplied `live_path`, unlike the copy_install
leg (`_handle_copy_install`), which already resolved+verified `live_path` before
touching it. `git checkout <ref>` / `git checkout <ref> -- <f>` rewrites the
index AND the worktree, so an unguarded git-managed leg could fetch/checkout
inside ANY directory the registry names — including a live source working tree.

`_resolve_contained_live_path` is the single shared helper both legs now call:
  1. live_path must resolve (strict) to an existing directory.
  2. resolved live_path must be under the resolved coordinator plugins dir
     (`Path.is_relative_to` semantics — not a substring test).
  3. resolved live_path must not equal resolved source_path, UNLESS
     propagation_mode == "source_is_live".
No fallback, no warn-and-continue: every refusal returns None.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    """Load refresh-plugin-live-install.py by file path (hyphenated name bypass)."""
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
    """_handle_copy_install must reject a live_path outside the plugins dir via
    the shared helper, matching pre-existing behaviour after the refactor that
    replaced its inline guard with a call to _resolve_contained_live_path."""
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

"""
coordinator/lib/resolve-claude-klabauter/tests/test_dispatch_prefers_stamped_engine.py

Chunk C5 (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md): pins
the new rule at its chokepoint — "an engine root is a stamped build. No
stamp, no engine." — with OUR OWN test, synthetic and in-process, never
depending on DoE's conformance fixture. That independence was originally
motivated by a fixture-side gap (no case created a stamped
`<KLABAUTER>/coordinator_core/_engine_stamp`); DoE closed that gap on
2026-08-29 with fixture schema 1.1.0's `paths.existing_files`, and the
driver's `_XFAIL_STAMP_RULE_REASON` table retired with it. The independence
is kept on its own merits — this test pins the chokepoint directly and must
not start depending on a fixture owned by another repo.

Negative-spec:
  - Does NOT touch `resolve_claude_klabauter_bin_dir()` — that stays single-tier,
    live-tree-only (DR-326's locator axis), deliberately un-flipped.
  - Does NOT assert anything about `compute_client_token`'s fallback removal
    (C4) or `exec_cli`'s C4b per-target fallback (C13) — both are separate
    chunks with their own tests.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_SHIM_PATH = Path(__file__).resolve().parent.parent / "_resolve_claude_klabauter.py"


def _load_shim():
    spec = importlib.util.spec_from_file_location("_c5_stamp_rule_shim", _SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_stamp(root: Path, body: str = "sha:test-c5-stamp\n") -> None:
    stamp = root / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(body, encoding="utf-8")


@pytest.fixture
def _registry(tmp_path, monkeypatch):
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    return SimpleNamespace(ml_dir=ml_dir, settings_home=settings_home)


def _register_published(ml_dir: Path, published_root: Path) -> None:
    (ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root.as_posix()}\'\n',
        encoding="utf-8",
    )


def test_unstamped_published_root_is_not_usable(tmp_path, _registry):
    published_root = tmp_path / "published"
    (published_root / "coordinator_core").mkdir(parents=True)

    _register_published(_registry.ml_dir, published_root)
    shim = _load_shim()

    assert shim._resolve_published_engine(_registry.ml_dir) is None


def test_empty_stamp_file_is_not_usable(tmp_path, _registry):
    published_root = tmp_path / "published"
    (published_root / "coordinator_core").mkdir(parents=True)
    (published_root / "coordinator_core" / "_engine_stamp").write_text(
        "", encoding="utf-8"
    )

    _register_published(_registry.ml_dir, published_root)
    shim = _load_shim()

    assert shim._resolve_published_engine(_registry.ml_dir) is None


def test_stamped_published_root_is_usable(tmp_path, _registry):
    published_root = tmp_path / "published"
    (published_root / "coordinator_core").mkdir(parents=True)
    _write_stamp(published_root)

    _register_published(_registry.ml_dir, published_root)
    shim = _load_shim()

    assert shim._resolve_published_engine(_registry.ml_dir) == published_root.as_posix()


def test_dispatch_denies_unstamped_engine_falls_through_to_live_tree(tmp_path, _registry, monkeypatch):
    published_root = tmp_path / "published"
    (published_root / "coordinator_core").mkdir(parents=True)

    live_root = tmp_path / "live"
    live_root.mkdir()

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_dir))

    other_working_dir = tmp_path / "other-working-repo"
    other_working_dir.mkdir()
    (_registry.ml_dir / "registry.local.toml").write_text(
        "[repos]\n"
        f"claude_klabauter = '{published_root.as_posix()}'\n"
        f"claude_klabauter = '{live_root.as_posix()}'\n"
        "\n"
        "[engine.working_repos]\n"
        f"other = '{other_working_dir.as_posix()}'\n",
        encoding="utf-8",
    )

    shim = _load_shim()
    root, resolution_class = shim.resolve_claude_klabauter_root_with_class()

    assert resolution_class == shim.RESOLUTION_LIVE_WORKING_TREE
    assert root == live_root.as_posix()


def test_dispatch_denies_unstamped_engine_raises_when_no_live_tree(tmp_path, _registry):
    published_root = tmp_path / "published"
    (published_root / "coordinator_core").mkdir(parents=True)

    (_registry.ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root.as_posix()}\'\n',
        encoding="utf-8",
    )

    shim = _load_shim()
    with pytest.raises(shim.ClaudeKlabauterResolutionError):
        shim.resolve_claude_klabauter_root_with_class()


def test_dispatch_uses_stamped_engine_as_last_resort(tmp_path, _registry):
    published_root = tmp_path / "published"
    (published_root / "coordinator_core").mkdir(parents=True)
    _write_stamp(published_root)

    (_registry.ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{published_root.as_posix()}\'\n',
        encoding="utf-8",
    )

    shim = _load_shim()
    root, resolution_class = shim.resolve_claude_klabauter_root_with_class()

    assert resolution_class == shim.RESOLUTION_RESOLVED_ENGINE
    assert root == published_root.as_posix()


def test_resolve_claude_klabauter_bin_dir_locator_axis_unaffected_by_stamp(tmp_path, _registry):
    live_root = tmp_path / "live"
    live_root.mkdir()
    (_registry.ml_dir / "registry.local.toml").write_text(
        f'"repos.claude_klabauter" = \'{live_root.as_posix()}\'\n',
        encoding="utf-8",
    )

    shim = _load_shim()
    with pytest.raises(shim.ClaudeKlabauterResolutionError) as excinfo:
        shim.resolve_claude_klabauter_bin_dir()

    assert "coordinator/bin" in str(excinfo.value)

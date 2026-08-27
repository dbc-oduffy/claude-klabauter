"""coordinator_core/install/tests/test_engine_root_for_install.py

Spec backlink: state/dispatch-briefs/2026-08-22-warm-engine-and-door-install-from-published-root/C1.md

Asserts `resolve_engine_root_for_install()` is a pure COMPOSITION of the two
existing resolvers — never a third derivation — by pinning non-divergence
against each of them directly, not merely against its own return shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import engine_root as engine_root_module
from coordinator_core.install.engine_root_for_install import (
    resolve_engine_root_for_install,
)
from coordinator_core.warm import engine_root as warm_engine_root_module


def _write_engine_stamp(root: Path) -> None:
    """Mirrors `skew.write_engine_stamp`'s one-line, bytes-only contract —
    see `test_engine_root_two_tier.py`'s identical fixture helper for why
    this is duplicated rather than imported (self-contained fixture)."""
    stamp = Path(root) / "coordinator_core" / "_engine_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("sha:test-fixture-stamp\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_shim_memo():
    """`published_engine_mirror_path()` loads C3's shim, memoized module-scope
    — reset around every test so no test's resolution pins the answer for a
    later one (mirrors `test_engine_root_two_tier.py`'s own reset fixture)."""
    engine_root_module._reset_shim_cache()
    yield
    engine_root_module._reset_shim_cache()


def _write_registry(ml_dir: Path, published_dir: "Path | None") -> None:
    lines = ["[repos]"]
    if published_dir is not None:
        lines.append(f'claude_klabauter = "{published_dir.as_posix()}"')
    lines.append("")
    (ml_dir / "registry.local.toml").write_text("\n".join(lines), encoding="utf-8")


def test_published_case_composes_published_engine_mirror_path(tmp_path, monkeypatch):
    """AC — published branch: kind="published", root equals
    `published_engine_mirror_path()`'s own answer, never re-derived."""
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)

    published_dir = tmp_path / "published"
    (published_dir / "coordinator_core").mkdir(parents=True)
    _write_engine_stamp(published_dir)

    _write_registry(ml_dir, published_dir)

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)

    from coordinator_core.engine_root import published_engine_mirror_path

    expected = published_engine_mirror_path()
    assert expected is not None

    result = resolve_engine_root_for_install()

    assert result.kind == "published"
    assert result.root == Path(expected)
    assert result.remediation is None


def test_klabauter_front_case_composes_current_engine_clone(tmp_path, monkeypatch):
    """AC — self-rooted branch (klabauter-front case): no published root
    registered, but the caller's OWN root is a stamped engine root — kind=
    "self-rooted", root equals `current_engine_clone()`'s own answer. Uses a
    monkeypatched `current_engine_clone()` rather than stamping the real live
    checkout (which is deliberately unstamped — the claude-klabauter-dev case below),
    since this module composes onto whatever that function returns rather
    than re-deriving the anchor itself."""
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)
    _write_registry(ml_dir, None)

    self_root = tmp_path / "self-mirror"
    _write_engine_stamp(self_root)

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.setattr(warm_engine_root_module, "current_engine_clone", lambda: self_root)

    from coordinator_core.engine_root import published_engine_mirror_path

    assert published_engine_mirror_path() is None

    result = resolve_engine_root_for_install()

    assert result.kind == "self-rooted"
    assert result.root == warm_engine_root_module.current_engine_clone()
    assert result.root == self_root
    assert result.remediation is None


def test_claude_klabauter_dev_case_self_unstamped_falls_to_none(tmp_path, monkeypatch):
    """AC — the claude-klabauter-dev case: no published root registered, and the
    caller's own root is NOT stamped (this repo's live dev tree today) —
    typed "no published engine" result, not a raise."""
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)
    _write_registry(ml_dir, None)

    unstamped_self = tmp_path / "unstamped-self"
    unstamped_self.mkdir()

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    monkeypatch.setattr(
        warm_engine_root_module, "current_engine_clone", lambda: unstamped_self
    )

    from coordinator_core.engine_root import published_engine_mirror_path

    assert published_engine_mirror_path() is None
    assert not warm_engine_root_module.is_engine_root(unstamped_self)

    result = resolve_engine_root_for_install()

    assert result.kind == "none"
    assert result.root is None
    assert result.remediation is not None
    assert "machine-local set repos.claude_klabauter" in result.remediation


def test_no_published_engine_registry_key_absent_or_unstamped(tmp_path, monkeypatch):
    """AC — the "no published engine on this box" outcome: registry key
    absent AND self unstamped -> typed no-engine result, never a raise."""
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)
    _write_registry(ml_dir, None)

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)

    unstamped_self = tmp_path / "another-unstamped-self"
    unstamped_self.mkdir()
    monkeypatch.setattr(
        warm_engine_root_module, "current_engine_clone", lambda: unstamped_self
    )

    result = resolve_engine_root_for_install()

    assert result.kind == "none"
    assert result.root is None
    assert result.remediation is not None

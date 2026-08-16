"""
coordinator/lib/resolve-claude-klabauter/tests/test_engine_target_fact.py

Chunk C3 (docs/plans/2026-08-16-one-engine-for-the-whole-box.md): the
box-wide ``engine.target`` fact — one declared value, resolvable through
``~/.coordinator-claude-settings``, exactly two values (``main``,
``candidate``). No per-repo axis exists to set this.

Covers, against the shim's own ``resolve_engine_target``:
  - both declared values readable in the nested ``[engine] target = "..."``
    TOML shape and the flat quoted-dotted-key shape ``machine-local set``
    writes;
  - ``registry.local.toml`` winning over ``registry.toml`` on collision
    (same precedence as every other declared fact this module reads);
  - absent key -> ``None`` (AC20: a read-site default, not a third stored
    value);
  - an unreadable/invalid value (typo, stale config) -> ``None``, same
    disposition as absence — never raised, never encoded as a third state.

Also proves the HARD CONSTRAINT from C3's body: the fact lives inside the
same ``(registry.toml, registry.local.toml, .claude-klabauter-root)`` tuple
``coordinator_core.claude_klabauter_root._registry_mtime_pair`` already stats, so
writing it self-invalidates both ``_ROOT_MEMO`` and ``_GATE_MEMO`` by mtime
— no explicit reset call (``_reset_root_memo()`` is a test-only seam with
zero non-test callers, not the rollback mechanism).

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C3
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_SHIM_PATH = _REPO_ROOT / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"

_spec = importlib.util.spec_from_file_location("_resolve_claude_klabauter_engine_target_under_test", _SHIM_PATH)
assert _spec is not None and _spec.loader is not None
resolve_claude_klabauter = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = resolve_claude_klabauter
_spec.loader.exec_module(resolve_claude_klabauter)

from coordinator_core import claude_klabauter_root as claude_klabauter_root_mod  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_reads_main_from_nested_table(tmp_path: Path) -> None:
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    _write(ml_dir / "registry.toml", '[engine]\ntarget = "main"\n')

    assert resolve_claude_klabauter.resolve_engine_target(ml_dir) == resolve_claude_klabauter.ENGINE_TARGET_MAIN


def test_reads_candidate_from_flat_dotted_key(tmp_path: Path) -> None:
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    _write(ml_dir / "registry.toml", '"engine.target" = "candidate"\n')

    assert resolve_claude_klabauter.resolve_engine_target(ml_dir) == resolve_claude_klabauter.ENGINE_TARGET_CANDIDATE


def test_registry_local_wins_over_registry_toml(tmp_path: Path) -> None:
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    _write(ml_dir / "registry.toml", '[engine]\ntarget = "main"\n')
    _write(ml_dir / "registry.local.toml", '[engine]\ntarget = "candidate"\n')

    assert resolve_claude_klabauter.resolve_engine_target(ml_dir) == resolve_claude_klabauter.ENGINE_TARGET_CANDIDATE


def test_absent_key_returns_none(tmp_path: Path) -> None:
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    _write(ml_dir / "registry.toml", '[repos]\nclaude_klabauter = "fake-claude-klabauter"\n')

    assert resolve_claude_klabauter.resolve_engine_target(ml_dir) is None


def test_missing_registry_files_returns_none(tmp_path: Path) -> None:
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()

    assert resolve_claude_klabauter.resolve_engine_target(ml_dir) is None


def test_invalid_value_is_treated_as_absent_never_a_third_state(tmp_path: Path) -> None:
    """AC20: an unreadable/typo'd value never diverts and is never encoded
    as a stored third value -- it collapses to the same ``None`` as absence,
    not a raise and not e.g. ``"unknown"``."""
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    _write(ml_dir / "registry.toml", '[engine]\ntarget = "nightly"\n')

    assert resolve_claude_klabauter.resolve_engine_target(ml_dir) is None


def test_default_ml_dir_argument_uses_shim_resolver(monkeypatch, tmp_path: Path) -> None:
    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    _write(ml_dir / "registry.toml", '[engine]\ntarget = "candidate"\n')
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))

    assert resolve_claude_klabauter.resolve_engine_target() == resolve_claude_klabauter.ENGINE_TARGET_CANDIDATE


class TestWritingTargetInvalidatesBothMemos:
    """HARD CONSTRAINT proof: `engine.target` lives in `registry.local.toml`,
    one of the three files `_registry_mtime_pair` stats -- so writing it
    changes the memo key both `_ROOT_MEMO` and `_GATE_MEMO` are keyed on,
    with no explicit reset call."""

    def setup_method(self) -> None:
        claude_klabauter_root_mod._reset_root_memo()
        claude_klabauter_root_mod._reset_gate_memo()

    def teardown_method(self) -> None:
        claude_klabauter_root_mod._reset_root_memo()
        claude_klabauter_root_mod._reset_gate_memo()

    def test_mtime_pair_changes_and_stale_memo_entries_are_unreachable(self, tmp_path: Path) -> None:
        ml_dir = tmp_path / "machine-local"
        ml_dir.mkdir()
        fake_root = "fake-claude-klabauter"
        _write(ml_dir / "registry.local.toml", f'[repos]\nclaude_klabauter = "{fake_root}"\n')

        old_key = claude_klabauter_root_mod._registry_mtime_pair(ml_dir)

        # Seed both memos as if a prior call had already resolved and cached
        # under the pre-write registry state.
        claude_klabauter_root_mod._ROOT_MEMO[old_key] = fake_root
        gate_key = (*old_key, None)
        claude_klabauter_root_mod._GATE_MEMO[gate_key] = (fake_root, "live-working-tree")

        assert claude_klabauter_root_mod._ROOT_MEMO.get(old_key) is not None
        assert claude_klabauter_root_mod._GATE_MEMO.get(gate_key) is not None

        # Force a distinguishable mtime bump (some filesystems have coarse
        # mtime granularity -- back-date the pre-write stat, then write, so
        # the two are unambiguously different ticks regardless of clock
        # resolution).
        registry_local = ml_dir / "registry.local.toml"
        stat_before = registry_local.stat()
        backdated = stat_before.st_mtime - 5.0
        os.utime(registry_local, (backdated, backdated))
        old_key = claude_klabauter_root_mod._registry_mtime_pair(ml_dir)
        claude_klabauter_root_mod._ROOT_MEMO.clear()
        claude_klabauter_root_mod._GATE_MEMO.clear()
        claude_klabauter_root_mod._ROOT_MEMO[old_key] = fake_root
        claude_klabauter_root_mod._GATE_MEMO[(*old_key, None)] = (fake_root, "live-working-tree")

        # Write the engine.target fact -- the only edit this row makes to
        # the registry state.
        _write(
            registry_local,
            f'[repos]\nclaude_klabauter = "{fake_root}"\n\n[engine]\ntarget = "candidate"\n',
        )

        new_key = claude_klabauter_root_mod._registry_mtime_pair(ml_dir)
        new_gate_key = (*new_key, None)

        assert new_key != old_key, (
            "writing engine.target did not change _registry_mtime_pair -- "
            "the memo would not self-invalidate"
        )
        # No explicit reset call happened between the write and these reads
        # -- the stale entries are simply unreachable under the new key,
        # which is the entire mechanism (no _reset_root_memo() call above).
        assert claude_klabauter_root_mod._ROOT_MEMO.get(new_key) is None
        assert claude_klabauter_root_mod._GATE_MEMO.get(new_gate_key) is None

        # And the fact itself is now readable through the shim.
        assert resolve_claude_klabauter.resolve_engine_target(ml_dir) == resolve_claude_klabauter.ENGINE_TARGET_CANDIDATE

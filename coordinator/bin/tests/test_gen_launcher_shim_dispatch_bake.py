"""test_gen_launcher_shim_dispatch_bake.py — round-trip + freshness coverage
for the C15 dispatch-root launcher cache (`gen-launcher-shim.py`'s
`write_dispatch_root_cache` / `read_dispatch_root_cache`,
`coordinator_core.install.substrate`'s mirrored `_write_dispatch_root_bake`).

Spec backlink: docs/plans/2026-08-21-the-cli-bootstrap-tax-dies-at-the-
interpreter-floor.md § C15 / AC17, AC18.

WHAT THIS COVERS
    - A write followed by a read with an unchanged composite key (engine
      build stamp bytes + `_registry_mtime_pair`'s float triple) is a HIT.
    - Any mutation of the registry mtime triple (a `machine-local set
      repos.*`-shaped edit) WITHOUT touching the stamp invalidates the
      cache -- HARD CONSTRAINT 2, the whole substance of this row's fork
      (AC17).
    - A stamp-only mutation (with the registry untouched) also invalidates
      the cache -- DR-328's own invalidation axis.
    - `LOCALAPPDATA` unset degrades to a clean no-op miss on read and a
      silent no-op on write -- never raises.
    - `coordinator_core.install.substrate`'s write helper produces a payload
      `gen-launcher-shim.py`'s reader accepts as a HIT -- the two mirrored
      (not imported) implementations must agree on shape.
    - The freshness comparison happens in this Python reader, never in any
      generated `.cmd`/`.ps1` body -- `render_cmd`/`render_ps1` output is
      unaffected by this mechanism's existence (AC18's byte-parity
      precondition: no generated launcher body changes).

NEGATIVE SPEC
    - Does NOT drive `install-substrate.py`'s full `run()` (network/venv/
      write-surface side effects far outside this row's scope) -- exercises
      `_write_dispatch_root_bake` directly against a fixture engine root.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_GEN_LAUNCHER_SHIM_PATH = REPO_ROOT / "coordinator" / "bin" / "gen-launcher-shim.py"


def _load_gen_launcher_shim():
    """Import `coordinator/bin/gen-launcher-shim.py` as a module.

    Mirrors `coordinator_core/test_bin_launcher_parity.py::_load_gen_launcher_shim`
    -- SourceFileLoader dance because the filename is hyphenated and carries
    no importable path.
    """
    spec = importlib.util.spec_from_file_location(
        "gen_launcher_shim", _GEN_LAUNCHER_SHIM_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_launcher_shim"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gen():
    return _load_gen_launcher_shim()


@pytest.fixture
def fixture_tree(tmp_path):
    """A minimal engine-root + machine-local-registry-dir fixture pair.

    `engine_root/coordinator_core/_engine_stamp` and `ml_dir/registry.toml`
    are the two files the composite key is derived from -- both start
    present so a mutation of either is meaningful (an absent file's mtime
    key is a stable `-1.0`, which can't demonstrate invalidation on its own
    the way an actual mtime bump can)."""
    engine_root = tmp_path / "engine"
    (engine_root / "coordinator_core").mkdir(parents=True)
    (engine_root / "coordinator_core" / "_engine_stamp").write_text("sha:abc123\n")

    ml_dir = tmp_path / "machine-local"
    ml_dir.mkdir()
    (ml_dir / "registry.toml").write_text("[repos]\nclaude_klabauter = '/x'\n")

    return engine_root, ml_dir


@pytest.fixture
def localappdata(tmp_path, monkeypatch):
    local = tmp_path / "localappdata"
    local.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return local


def test_write_then_read_same_key_is_a_hit(gen, fixture_tree, localappdata):
    engine_root, ml_dir = fixture_tree
    gen.write_dispatch_root_cache(ml_dir, engine_root, str(engine_root), "resolved-engine")

    result = gen.read_dispatch_root_cache(ml_dir, engine_root)

    assert result == (str(engine_root), "resolved-engine")


def test_read_before_any_write_is_a_miss(gen, fixture_tree, localappdata):
    engine_root, ml_dir = fixture_tree

    assert gen.read_dispatch_root_cache(ml_dir, engine_root) is None


def test_registry_mtime_mutation_without_stamp_change_invalidates(gen, fixture_tree, localappdata):
    """HARD CONSTRAINT 2 / AC17: a `machine-local set repos.*`-shaped edit
    (registry.toml rewritten, stamp untouched) must invalidate the cache --
    a stamp-only key would silently serve the pre-edit root."""
    engine_root, ml_dir = fixture_tree
    gen.write_dispatch_root_cache(ml_dir, engine_root, str(engine_root), "resolved-engine")
    assert gen.read_dispatch_root_cache(ml_dir, engine_root) is not None

    registry = ml_dir / "registry.toml"
    registry.write_text(registry.read_text() + "\n# redirected\n")
    import os
    import time

    later = time.time() + 5
    os.utime(registry, (later, later))

    assert gen.read_dispatch_root_cache(ml_dir, engine_root) is None


def test_stamp_mutation_without_registry_change_invalidates(gen, fixture_tree, localappdata):
    """DR-328's own invalidation axis: a new engine build stamp alone must
    also miss, independent of the registry mtime triple."""
    engine_root, ml_dir = fixture_tree
    gen.write_dispatch_root_cache(ml_dir, engine_root, str(engine_root), "resolved-engine")
    assert gen.read_dispatch_root_cache(ml_dir, engine_root) is not None

    (engine_root / "coordinator_core" / "_engine_stamp").write_text("sha:def456\n")

    assert gen.read_dispatch_root_cache(ml_dir, engine_root) is None


def test_localappdata_unset_is_a_clean_noop(gen, fixture_tree, monkeypatch):
    engine_root, ml_dir = fixture_tree
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    gen.write_dispatch_root_cache(ml_dir, engine_root, str(engine_root), "resolved-engine")
    assert gen.read_dispatch_root_cache(ml_dir, engine_root) is None


def test_corrupt_cache_file_is_a_miss_not_an_error(gen, fixture_tree, localappdata):
    engine_root, ml_dir = fixture_tree
    path = gen.dispatch_root_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{")

    assert gen.read_dispatch_root_cache(ml_dir, engine_root) is None


def test_write_persists_no_tracked_cmd_or_ps1_body_change(gen, fixture_tree, localappdata):
    """AC18 precondition: this mechanism's presence must not alter a single
    byte the byte-parity guard checks -- `render_cmd`/`render_ps1` output is
    unaffected by whether a dispatch-root bake has ever run."""
    before_cmd = gen.render_cmd("some-entrypoint.py")
    before_ps1 = gen.render_ps1("some-entrypoint.py")

    engine_root, ml_dir = fixture_tree
    gen.write_dispatch_root_cache(ml_dir, engine_root, str(engine_root), "resolved-engine")

    after_cmd = gen.render_cmd("some-entrypoint.py")
    after_ps1 = gen.render_ps1("some-entrypoint.py")

    assert before_cmd == after_cmd
    assert before_ps1 == after_ps1


def test_substrate_write_helper_agrees_with_gen_launcher_shim_reader(fixture_tree, localappdata):
    """The two MIRRORED (not imported) implementations
    (`coordinator_core.install.substrate._write_dispatch_root_bake` and
    `gen-launcher-shim.py`'s `read_dispatch_root_cache`) must agree on
    on-disk shape -- a write from one side is a hit from the other."""
    from coordinator_core.install import substrate

    engine_root, ml_dir = fixture_tree
    substrate.machine_local_dir = lambda: ml_dir  # type: ignore[assignment]
    try:
        substrate._write_dispatch_root_bake(engine_root, str(engine_root), "resolved-engine")
    finally:
        del substrate.machine_local_dir

    gen = _load_gen_launcher_shim()
    result = gen.read_dispatch_root_cache(ml_dir, engine_root)

    assert result == (str(engine_root), "resolved-engine")


def test_substrate_write_helper_localappdata_unset_is_noop(fixture_tree, monkeypatch):
    from coordinator_core.install import substrate

    engine_root, _ml_dir = fixture_tree
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    substrate._write_dispatch_root_bake(engine_root, str(engine_root), "resolved-engine")


def test_cache_payload_shape_is_stamp_and_registry_mtime_and_root(gen, fixture_tree, localappdata):
    engine_root, ml_dir = fixture_tree
    gen.write_dispatch_root_cache(ml_dir, engine_root, str(engine_root), "resolved-engine")

    payload = json.loads(gen.dispatch_root_cache_path().read_text(encoding="utf-8"))

    assert set(payload) == {"stamp", "registry_mtime", "root", "resolution_class"}
    assert isinstance(payload["registry_mtime"], list)
    assert len(payload["registry_mtime"]) == 3

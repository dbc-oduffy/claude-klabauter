"""
Tests for coordinator_core's PEP 562 lazy re-export __getattr__.

Purpose: prove the ratified cross-repo import contract (DR § AC-1b — six names
importable both as `from coordinator_core import X` and `coordinator_core.X`)
survives making the cache and authz.token re-exports lazy, and that the write
surface (write_tokens/generate_token) stays unreachable through this module.

Spec backlink: dispatch brief "Cut the cold-import cost of
coordinator_core/__init__.py" (2026-07-27), coordinator_core/__init__.py.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_star_import_style_from_import_resolves_all_six_names():
    from coordinator_core import (
        OP_KEY_SCOPE,
        WORKTREE_SCOPED_OPS,
        compute_stamp,
        read_revalidated,
        read_token,
        read_token_ro,
    )

    assert compute_stamp is not None
    assert read_revalidated is not None
    assert read_token is not None
    assert read_token_ro is not None
    assert OP_KEY_SCOPE is not None
    assert WORKTREE_SCOPED_OPS is not None


def test_attribute_access_resolves_all_six_names():
    import coordinator_core

    assert coordinator_core.compute_stamp is not None
    assert coordinator_core.read_revalidated is not None
    assert coordinator_core.read_token is not None
    assert coordinator_core.read_token_ro is not None
    assert coordinator_core.OP_KEY_SCOPE is not None
    assert coordinator_core.WORKTREE_SCOPED_OPS is not None


def test_lazy_names_resolve_to_the_owning_module_objects():
    import coordinator_core
    import coordinator_core.authz.token as token_mod
    import coordinator_core.cache as cache_mod

    assert coordinator_core.compute_stamp is cache_mod.compute_stamp
    assert coordinator_core.read_revalidated is cache_mod.read_revalidated
    assert coordinator_core.read_token is token_mod.read_token
    assert coordinator_core.read_token_ro is token_mod.read_token_ro


def test_write_surface_is_not_promoted():
    import coordinator_core

    for bad_name in ("write_tokens", "generate_token", "not_a_real_attr"):
        try:
            getattr(coordinator_core, bad_name)
        except AttributeError:
            pass
        else:
            raise AssertionError(f"expected AttributeError for {bad_name!r}")


def test_dir_surfaces_all_names_in___all__():
    import coordinator_core

    names = dir(coordinator_core)
    for name in coordinator_core.__all__:
        assert name in names


def test_import_coordinator_core_does_not_pull_cache_or_authz_token_eagerly():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import coordinator_core, sys\n"
                "assert 'coordinator_core.cache' not in sys.modules, "
                "'coordinator_core.cache eagerly imported'\n"
                "assert 'coordinator_core.authz.token' not in sys.modules, "
                "'coordinator_core.authz.token eagerly imported'\n"
            ),
        ],
        capture_output=True,
        text=True,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_accessing_lazy_name_then_pulls_owning_module_into_sys_modules():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import coordinator_core, sys\n"
                "coordinator_core.read_token\n"
                "assert 'coordinator_core.authz.token' in sys.modules\n"
            ),
        ],
        capture_output=True,
        text=True,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )

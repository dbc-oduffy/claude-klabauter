"""
conftest.py — tree-wide machine-local registry isolation, plus shared fixtures
and helpers for cruft-sweep tests.

Spec backlink: docs/plans/2026-06-09-distill-cruft-sweep.md § C1/C2

Review: code-reviewer (F9) — run(), bp(), and _make_stale_uuid_dir() were
copy-pasted across 4+ test files; extracted here so updates land in one place.
Individual test files retain their own definitions for backward compatibility
(import from conftest is opt-in); this file provides them as pytest-importable
helpers and as fixtures where appropriate.

Isolation: helpers never touch real ~/.claude/ state; they operate exclusively
on tmp_path fixtures passed by callers. The two autouse fixtures below extend
that guarantee from a per-helper discipline to a tree-wide one for the ONE
piece of machine state this tree can reach and no other hygiene notices — the
live machine-local registry. See
``coordinator_core.testing.registry_sandbox`` for the 2026-07-28 incident that
motivated them and for why ``MACHINE_LOCAL_REGISTRY_DIR`` (rather than
``COORDINATOR_SETTINGS_HOME``) is the correct lever.
"""

import os
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.testing.registry_sandbox import fail_on_live_registry_write_fixture


# Autouse across this whole tree, and deliberately DETECTION rather than
# redirection. The obvious prevention — arming ``MACHINE_LOCAL_REGISTRY_DIR``
# registry ladder, above the ``CLAUDE_HOME``/``COORDINATOR_SETTINGS_HOME``
# silently OVERRIDES the isolation such tests already set up for their
# subprocesses (``env = dict(os.environ); env["CLAUDE_HOME"] = tmp``). Six
_fail_on_live_registry_write = pytest.fixture(autouse=True)(fail_on_live_registry_write_fixture)


# into warmth via the machine-local registry rung (unset `COORDINATOR_WARM`
# `COORDINATOR_WARM=0` always wins over the registry rung (`warm/settings.py`
@pytest.fixture(autouse=True)
def _pin_warm_disabled_for_cli_driving_tests(monkeypatch):
    monkeypatch.setenv("COORDINATOR_WARM", "0")


# NEGATIVE SPEC: this conftest holds no process-spawning helper, and must not


@pytest.fixture(autouse=True)
def _reclaim_publish_shadow_trees():
    yield
    for module in list(sys.modules.values()):
        cache = getattr(module, "_MATERIALIZED_REF_CACHE", None)
        cleanup = getattr(module, "_cleanup_shadow_roots", None)
        if cache is None or cleanup is None or not cache:
            continue
        cleanup(tuple(cache.values()))
        cache.clear()


def _make_stale_uuid_dir(parent: Path, uuid: str, age_days: int = 15) -> Path:
    d = parent / uuid
    d.mkdir(parents=True, exist_ok=True)
    (d / "dummy.txt").write_text("session data")
    old_mtime = time.time() - age_days * 86400
    os.utime(d, (old_mtime, old_mtime))
    return d

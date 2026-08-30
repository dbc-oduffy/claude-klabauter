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

# `RealIdentityCheckMixin` (shared fake-`ClaudeKlabauterPercolate` `run_identity_check`
# stand-in) deliberately does NOT live here: this file is named `conftest.py`,
# and `coordinator/bin/` carries its own same-named `conftest.py` -- a plain
# `from conftest import X` resolves through `sys.modules["conftest"]`, which a
# combined pytest run spanning both directories has already bound to whichever
# of the two pytest auto-imported first (collection order, not import-site
# proximity). Verified live: `pytest coordinator/tests/test_percolate_driver_
# gates.py coordinator/bin/tests/test_percolate_identity_check_gate.py`
# resolved `from conftest import RealIdentityCheckMixin` against
# `coordinator/bin/conftest.py` and raised `ImportError`. See
# `_fake_claude_klabauter_identity.py` (uniquely named, same directory, same import
# idiom as `_repo_paths.py` already used by these test files) instead.

# Autouse across this whole tree, and deliberately DETECTION rather than
# redirection. The obvious prevention — arming ``MACHINE_LOCAL_REGISTRY_DIR``
# for every test — was tried and rejected: that variable is rung 1 of the
# registry ladder, above the ``CLAUDE_HOME``/``COORDINATOR_SETTINGS_HOME``
# rung that this tree's existing isolation idiom uses, so an ambient value
# silently OVERRIDES the isolation such tests already set up for their
# subprocesses (``env = dict(os.environ); env["CLAUDE_HOME"] = tmp``). Six
# tests across four files broke on it. A guard that changes what passing tests
# resolve is not a guard. Shared implementation: see
# ``coordinator_core.testing.registry_sandbox.fail_on_live_registry_write_fixture``
# (Review: code-reviewer, Finding 3, 2026-07-28 — was a byte-identical copy
# duplicated with ``coordinator/bin/conftest.py``; factored into one place).
_fail_on_live_registry_write = pytest.fixture(autouse=True)(fail_on_live_registry_write_fixture)

# NEGATIVE SPEC: this conftest holds no process-spawning helper, and must not
# regain one. A conftest cannot carry `@pytest.mark.cadence` — a marker only
# tiers the test that declares it — so a spawn site here is untierable by
# construction and lands on whatever tier its importers run at. The
# `SCRIPT`/`run()`/`bp()` cruft-sweep helpers that used to live here were
# removed once their callers were excised (2026-08-07 cull); a spawning helper
# belongs in a uniquely-named sibling module, per the import-shadowing note
# above.


@pytest.fixture(autouse=True)
def _reclaim_publish_shadow_trees():
    """Reclaim shadow trees any test materialized, across every loaded publish module.

    `publish.py`'s `_extract_git_archive` extracts into `tempfile.mkdtemp()` —
    real OS temp, NOT pytest's `tmp_path` — so pytest teardown never removes
    these and they accumulate indefinitely on a long-lived CI box.

    Tree-wide for the same reason the registry fixtures above are: the shadow
    escapes through several seams (`_git_materialize_ref` directly,
    `run_pre_sync_gates` returning `GateResult.shadow_roots` a test never
    reclaims, `process_target` called without a `shadow_roots_sink`) and across
    several test files. Scoping it to the classes or the one file that
    obviously materialize leaves the non-obvious callers leaking — measured,
    not assumed: fixing it per-class left 2 leaks per run, per-file left 3.

    Each test file loads `publish.py` under its own module name via importlib,
    so the cache is per-instance; sweep every loaded instance rather than a
    single import. Reclamation goes exclusively through the modules' own cache
    values — never a glob over the temp prefix, which would delete the live
    shadow trees of a real publish running concurrently on this machine.
    """
    yield
    for module in list(sys.modules.values()):
        cache = getattr(module, "_MATERIALIZED_REF_CACHE", None)
        cleanup = getattr(module, "_cleanup_shadow_roots", None)
        if cache is None or cleanup is None or not cache:
            continue
        cleanup(tuple(cache.values()))
        cache.clear()


def _make_stale_uuid_dir(parent: Path, uuid: str, age_days: int = 15) -> Path:
    """Create a uuid directory under parent with mtime aged by age_days."""
    d = parent / uuid
    d.mkdir(parents=True, exist_ok=True)
    (d / "dummy.txt").write_text("session data")
    old_mtime = time.time() - age_days * 86400
    os.utime(d, (old_mtime, old_mtime))
    return d

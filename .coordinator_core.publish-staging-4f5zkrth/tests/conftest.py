"""
coordinator_core.tests conftest — pytest configuration for claude-klabauter-native hook op tests.

Ensures the project root (resolved via Path(__file__).parent.parent.parent) is importable
when pytest is invoked from coordinator_core/ or any subdirectory. Idempotent — no-op if
already on sys.path.

Also provides the _isolated_svc_root autouse fixture that sets COORDINATOR_SVC_ROOT to a
short per-test tmp dir so every test that exercises global_sentinel_dir() or
global_socket_path() (directly or via SingletonLock / ServiceContext) targets an isolated
path rather than the machine-global /tmp/coordinator-svc-<uid>/... path. Without this,
concurrent or sequential full-suite runs collide on the shared singleton lock and sentinel
files — causing RuntimeError "Sentinel not written within 25s" and flaky
test_singleton_lock_acquire_release failures.

Review: code-reviewer (B-F9) — replaced machine-specific absolute path in docstring with
the generic Path-resolution description that matches the actual implementation below.

Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C0 / D3
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path

import pytest

# Project root = three levels up from this file:
#   coordinator_core/tests/conftest.py → coordinator_core/tests/ → coordinator_core/ → <root>
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.resolve())
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Import after sys.path is set so coordinator_core is resolvable.
from coordinator_core.tests._fixtures import isolated_svc_root_impl  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_svc_root(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Redirect global singleton paths to a per-test tmpdir.

    Sets COORDINATOR_SVC_ROOT so coordinator_core.lifecycle.global_sentinel_dir() and
    global_socket_path() return test-isolated paths instead of the machine-global
    /tmp/coordinator-svc-<uid>/... paths. Prevents full-suite global-singleton contention.

    Path is kept intentionally short (prefix ccsvc- under /tmp) to stay within the macOS
    AF_UNIX sun_path limit of 103 usable bytes:
        /tmp/ccsvc-XXXXXXXX/coordinator.sock  →  ~34 chars, always < 103.

    Teardown removes the tmp dir (ignore_errors=True — subprocess may still be writing).

    Negative-spec:
      - Do NOT use pytest's tmp_path here — it produces deep paths under
        /tmp/pytest-<N>/test_<name>/<N>/ that may approach or exceed 103 bytes.

    Review: code-reviewer (F7, F4) — body extracted to _fixtures.isolated_svc_root_impl;
    return annotation corrected from pytest.fixture to Generator[None, None, None].
    """
    # Review: code-reviewer (F7) — implementation lives in _fixtures to avoid duplication
    # with coordinator_core/invoke/tests/conftest.py.
    yield from isolated_svc_root_impl(monkeypatch)

"""test_data_home_stamp_guard.py — regression net for the data-home /
code-root split.

Defect (2026-09-24): `cli_shared.claude_klabauter_root()` — the engine CODE-root
resolver — answered `COORDINATOR_ENGINE_ROOT` (a published mirror on a
resolved-engine box) before `machine-local get repos.claude_klabauter`. Two
callers used that same function as the DATA home for `state/` writes:
`coordinator-queue-append`'s central-scope `_output_path()` and
`coordinator-harvest-deferrals`'s `_resolved_claude_klabauter_root()` dedup scan. A
published mirror's `state/` is gitignored there, so an entry routed to it is
accepted, printed as a success, and silently never persisted.

`cli_shared.claude_klabauter_data_home()` closes this: registry (`repos.claude_klabauter`)
first, and an env override is honoured only when it does NOT carry the engine
build stamp (a stamped root is a published mirror, refused regardless of
env-var precedence).

Non-spawning, in-process unit tests only (`monkeypatch` on
`cli_shared._load_machine_local_kernel` / env vars / `os.path.getsize`) — no
new subprocess, per coordinator_core/tests/test_no_new_spawning_tests.py.

Run: python -m pytest coordinator/bin/tests/test_data_home_stamp_guard.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence]

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cli_shared  # noqa: E402


class _FakeKernel:
    """Minimal stand-in for `_load_machine_local_kernel()`'s return value —
    same shape as test_cli_shared_dump_repos_parity.py's `_FakeKernel`,
    trimmed to just `resolve_one` (the only primitive `machine_local_get`
    calls)."""

    EXIT_OK = 0
    EXIT_NOT_FOUND = 1

    def __init__(self, registry):
        self._registry = registry

    def resolve_one(self, key, layers):
        if key in self._registry:
            return (self.EXIT_OK, self._registry[key])
        return (self.EXIT_NOT_FOUND, None)


def _stub_registry(monkeypatch, registry):
    monkeypatch.setattr(cli_shared, "_load_machine_local_kernel", lambda: _FakeKernel(registry))


def _clear_env(monkeypatch):
    monkeypatch.delenv(cli_shared.COORDINATOR_ENGINE_ROOT_ENV, raising=False)
    monkeypatch.delenv(cli_shared.CLAUDE_KLABAUTER_ROOT_ENV, raising=False)


def test_data_home_prefers_registry_over_stamped_engine_root(monkeypatch, tmp_path):
    """AC: with COORDINATOR_ENGINE_ROOT pointing at a different (stamped)
    root and repos.claude_klabauter registered, claude_klabauter_data_home() resolves
    under repos.claude_klabauter — never the stamped mirror.
    """
    _clear_env(monkeypatch)
    stamped_root = tmp_path / "claude-klabauter"
    stamp_dir = stamped_root / "coordinator_core"
    stamp_dir.mkdir(parents=True)
    (stamp_dir / "_engine_stamp").write_text("stamped\n", encoding="utf-8")

    authoring_root = str(tmp_path / "claude-klabauter")
    monkeypatch.setenv(cli_shared.COORDINATOR_ENGINE_ROOT_ENV, str(stamped_root))
    _stub_registry(monkeypatch, {"repos.claude_klabauter": authoring_root})

    resolved = cli_shared.claude_klabauter_data_home()

    assert resolved == authoring_root
    assert resolved != str(stamped_root)


def test_data_home_falls_back_to_env_when_unstamped_and_unregistered(monkeypatch, tmp_path):
    """An unstamped env override (a dev-pointed fixture, no engine build
    stamp) is a legitimate data home when the registry has nothing."""
    _clear_env(monkeypatch)
    dev_root = tmp_path / "dev-checkout"
    dev_root.mkdir()
    monkeypatch.setenv(cli_shared.CLAUDE_KLABAUTER_ROOT_ENV, str(dev_root))
    _stub_registry(monkeypatch, {})

    assert cli_shared.claude_klabauter_data_home() == str(dev_root)


def test_data_home_unstamped_env_wins_over_registry(monkeypatch, tmp_path):
    """Same precedence as the native write seam, so a harvest dedup scan
    and the queue write it dedups resolve one root."""
    _clear_env(monkeypatch)
    dev_root = tmp_path / "dev-checkout"
    dev_root.mkdir()
    monkeypatch.setenv(cli_shared.COORDINATOR_ENGINE_ROOT_ENV, str(dev_root))
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(tmp_path / "claude-klabauter")})

    assert cli_shared.claude_klabauter_data_home() == str(dev_root)


def test_data_home_refuses_stamped_env_when_unregistered(monkeypatch, tmp_path):
    """A stamped override with NOTHING registered must resolve to None
    (graceful WARN+skip), never silently answer the published mirror."""
    _clear_env(monkeypatch)
    stamped_root = tmp_path / "claude-klabauter"
    stamp_dir = stamped_root / "coordinator_core"
    stamp_dir.mkdir(parents=True)
    (stamp_dir / "_engine_stamp").write_text("stamped\n", encoding="utf-8")
    monkeypatch.setenv(cli_shared.COORDINATOR_ENGINE_ROOT_ENV, str(stamped_root))
    _stub_registry(monkeypatch, {})

    assert cli_shared.claude_klabauter_data_home() is None


def test_claude_klabauter_root_code_root_resolver_unchanged_env_first(monkeypatch, tmp_path):
    """Negative-spec check: claude_klabauter_root() (the engine CODE-root resolver)
    must keep its existing env-first precedence — this fix must not touch
    that function's behaviour."""
    _clear_env(monkeypatch)
    stamped_root = tmp_path / "claude-klabauter"
    stamped_root.mkdir()
    monkeypatch.setenv(cli_shared.COORDINATOR_ENGINE_ROOT_ENV, str(stamped_root))
    _stub_registry(monkeypatch, {"repos.claude_klabauter": str(tmp_path / "claude-klabauter")})

    assert cli_shared.claude_klabauter_root() == str(stamped_root)

"""test_coordinator_lesson_add_meta_routing.py — hermetic env-driven coverage
of the meta-repo routing branch in coordinator-lesson-add._lessons_dir().

Targets the `_same_path(root, home)` conditional at coordinator-lesson-add:162,
which has two untested sub-cases when cwd's git root equals CLAUDE_HOME:

  (a) claude-klabauter resolvable    — CLAUDE_KLABAUTER_ROOT set → returns <CLAUDE_KLABAUTER_ROOT>/state/lessons
  (b) claude-klabauter unresolvable  — CLAUDE_KLABAUTER_ROOT unset AND machine-local resolution
                              disabled → _claude_klabauter_root() returns None →
                              falls back to <git-root>/state/lessons and emits
                              a stderr warning.

Unlike test_lesson_add.py's TestLessonsDirMetaRepoRouting (which mocks
_git_root/_claude_home/_claude_klabauter_root directly), this file drives the routing
through REAL env vars and a REAL git repo — closer to production behaviour,
and exercises _machine_local_get's OSError-catch fallback path via a
deliberately-nonexistent MACHINE_LOCAL_IMPL.

Converted from a hand-rolled unittest runner to collectable pytest functions.

Run: python3 -m pytest coordinator/bin/tests/test_coordinator_lesson_add_meta_routing.py

Spec backlink: state/bug-backlog/2026-07-06-lesson-add-dedup-scans-wrong-dir-in-meta.yaml
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import subprocess
import unittest.mock
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# ---------------------------------------------------------------------------
# Path setup — locate CLI relative to this test file
# test file: coordinator/bin/tests/test_coordinator_lesson_add_meta_routing.py
# CLI:       coordinator/bin/coordinator-lesson-add
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_CLI_PATH = _BIN_DIR / "coordinator-lesson-add.py"

# Load the CLI as a Python module for unit testing.
# The sh/python trampoline header (''''exec...) is inert when imported as Python.
_loader = importlib.machinery.SourceFileLoader("coordinator_lesson_add_meta_routing", str(_CLI_PATH))
_spec = importlib.util.spec_from_loader("coordinator_lesson_add_meta_routing", _loader)
_cli_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_cli_mod)

# Env vars this suite isolates (never leaked into the real process or across tests).
_ENV_VARS = (
    _cli_mod._QUEUE_APPEND_OUTPUT_ROOT_ENV,
    _cli_mod._CLAUDE_HOME_ENV,
    _cli_mod._CLAUDE_KLABAUTER_ROOT_ENV,
    _cli_mod._MACHINE_LOCAL_IMPL_ENV,
)


@pytest.fixture
def meta_repo_home(tmp_path, monkeypatch):
    """Real git-init'd tmp dir acting as both cwd and CLAUDE_HOME.

    Arranges _git_root() == _claude_home() by git-init'ing a tmp dir and
    running with cwd set to that same dir, so `git rev-parse --show-toplevel`
    resolves to it. CLAUDE_HOME is pointed at the same dir via env override.
    """
    for k in _ENV_VARS:
        monkeypatch.delenv(k, raising=False)

    fake_home = os.path.realpath(str(tmp_path))

    subprocess.run(
        ["git", "init", "--quiet", fake_home],
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )

    monkeypatch.chdir(fake_home)
    monkeypatch.setenv(_cli_mod._CLAUDE_HOME_ENV, fake_home)
    return fake_home


def test_claude_klabauter_resolvable_routes_to_claude_klabauter_state_lessons(meta_repo_home, tmp_path_factory, monkeypatch):
    """Sub-case (a): CLAUDE_KLABAUTER_ROOT set → _lessons_dir() routes to <CLAUDE_KLABAUTER_ROOT>/state/lessons."""
    fake_claude_klabauter = os.path.realpath(str(tmp_path_factory.mktemp("claude-klabauter")))
    monkeypatch.setenv(_cli_mod._CLAUDE_KLABAUTER_ROOT_ENV, fake_claude_klabauter)

    assert os.environ.get(_cli_mod._QUEUE_APPEND_OUTPUT_ROOT_ENV) is None, (
        "precondition: QUEUE_APPEND_OUTPUT_ROOT must be unset so it doesn't short-circuit"
    )

    result = _cli_mod._lessons_dir()

    expected = os.path.join(fake_claude_klabauter, "state", "lessons")
    assert result == expected, (
        f"meta-repo cwd with resolvable claude-klabauter must route to <claude-klabauter>/state/lessons, got {result!r}"
    )


def test_claude_klabauter_unresolvable_falls_back_to_git_root_with_warning(meta_repo_home, monkeypatch):
    """Sub-case (b): CLAUDE_KLABAUTER_ROOT unset AND machine-local resolution disabled →
    _claude_klabauter_root() returns None → falls back to <git-root>/state/lessons and
    emits a stderr warning.
    """
    fake_home = meta_repo_home
    # Point MACHINE_LOCAL_IMPL at a path that does not exist, so
    # _machine_local_get's subprocess.run raises OSError (caught -> None),
    # forcing _claude_klabauter_root() to return None without touching the real
    # ~/.claude/bin/_machine_local.py or the real machine-local registry.
    monkeypatch.setenv(
        _cli_mod._MACHINE_LOCAL_IMPL_ENV,
        os.path.join(fake_home, "nonexistent", "_machine_local.py"),
    )

    assert os.environ.get(_cli_mod._QUEUE_APPEND_OUTPUT_ROOT_ENV) is None, (
        "precondition: QUEUE_APPEND_OUTPUT_ROOT must be unset so it doesn't short-circuit"
    )
    assert os.environ.get(_cli_mod._CLAUDE_KLABAUTER_ROOT_ENV) is None, (
        "precondition: CLAUDE_KLABAUTER_ROOT must be unset for this sub-case"
    )

    captured_err = io.StringIO()
    with unittest.mock.patch("sys.stderr", captured_err):
        result = _cli_mod._lessons_dir()

    expected = os.path.join(fake_home, "state", "lessons")
    assert _cli_mod._same_path(result, expected), (
        f"unresolvable claude-klabauter must degrade gracefully to <git-root>/state/lessons, got {result!r}"
    )
    assert "claude-klabauter root unresolvable" in captured_err.getvalue(), (
        "unresolvable claude-klabauter must emit a warning to stderr"
    )

"""test_archive_session_scope.py — unit tests for coordinator/bin/archive-session-scope.py.

Coverage — the archive-session subcommand, which is the whole CLI:
    - missing --sid -> exit 1, no call
    - archive() raising -> non-fatal, exit 0 (docstring contract)
    - archive() returning False -> non-fatal, exit 0
    - archive() returning True -> exit 0, called with the right sid

The `_build_tail_args` suite that used to sit above these was removed
2026-08-30 alongside the `tail-args` subcommand itself: its only consumer,
`coordinator/bin/wsc-tail.py`, was retired by K-046 on 2026-08-23. Those tests
were green throughout — they were asserting the argv contract of a parser that
had not existed for a week, which is the shape a test takes when it outlives
its subject rather than the shape of coverage.

Module import: archive-session-scope.py is a hyphenated filename, loaded by file path
(same idiom as test_check_install_divergence.py / test-archive-stamp-cli-
ship-handoff.py in this same tests/ dir).

Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 (WSC-3 chunk).

Run:
    python -m pytest coordinator/bin/tests/test_archive_session_scope.py -q
"""
from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_archive_session_scope_module():
    spec = importlib.util.spec_from_file_location(
        "archive_session_scope_test_module", _BIN_DIR / "archive-session-scope.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_archive_session_scope = _load_archive_session_scope_module()


# ---------------------------------------------------------------------------
# _build_tail_args
# ---------------------------------------------------------------------------




def test_archive_session_missing_sid_exits_1():
    err = io.StringIO()
    with redirect_stderr(err):
        rc = _archive_session_scope.main(["archive-session", "--sid", ""])
    assert rc == 1
    assert "--sid required" in err.getvalue()


def test_archive_session_success_calls_archive_with_sid():
    fake_scope_mod = mock.MagicMock()
    fake_scope_mod.archive = mock.MagicMock(return_value=True)

    with mock.patch.object(
        _archive_session_scope, "require_colocated_engine_on_path", return_value="/fake/claude-klabauter/root"
    ):
        with mock.patch.dict(
            sys.modules, {"coordinator_core.session.scope": fake_scope_mod}
        ):
            rc = _archive_session_scope.main(["archive-session", "--sid", "sess-123"])

    assert rc == 0
    fake_scope_mod.archive.assert_called_once_with("sess-123")


def test_archive_session_false_return_is_non_fatal():
    fake_scope_mod = mock.MagicMock()
    fake_scope_mod.archive = mock.MagicMock(return_value=False)

    with mock.patch.object(
        _archive_session_scope, "require_colocated_engine_on_path", return_value="/fake/claude-klabauter/root"
    ):
        with mock.patch.dict(
            sys.modules, {"coordinator_core.session.scope": fake_scope_mod}
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = _archive_session_scope.main(["archive-session", "--sid", "sess-123"])

    assert rc == 0
    assert "non-fatal" in err.getvalue()


def test_archive_session_raising_is_non_fatal():
    fake_scope_mod = mock.MagicMock()
    fake_scope_mod.archive = mock.MagicMock(side_effect=RuntimeError("boom"))

    with mock.patch.object(
        _archive_session_scope, "require_colocated_engine_on_path", return_value="/fake/claude-klabauter/root"
    ):
        with mock.patch.dict(
            sys.modules, {"coordinator_core.session.scope": fake_scope_mod}
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = _archive_session_scope.main(["archive-session", "--sid", "sess-123"])

    assert rc == 0
    assert "boom" in err.getvalue()


def test_archive_session_claude_klabauter_root_unresolvable_is_non_fatal():
    with mock.patch.object(
        _archive_session_scope,
        "require_colocated_engine_on_path",
        side_effect=RuntimeError("no claude-klabauter checkout found"),
    ):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = _archive_session_scope.main(["archive-session", "--sid", "sess-123"])

    assert rc == 0
    assert "no claude-klabauter checkout found" in err.getvalue()

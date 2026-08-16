"""test_lesson_add.py — delegation and dedup pre-check tests for coordinator-lesson-add.

Tests (AC3, AC6 from strang-08 plan):
  AC3  coordinator-lesson-add inherits native routing via subprocess delegation
       to coordinator-queue-append; wrapper-layer dedup pre-check (contract pt 3)
       is still load-bearing and unchanged.
  AC6  Grep-gate asserting delegation and dedup structure intact; no independent
       routing gate added to lesson-add.

Native routing inheritance is transitive: lesson-add subprocess-calls
coordinator-queue-append, which (after C2) routes through cc_invoke to
queue.append when the seam is present. lesson-add itself holds no routing gate
and does not import cc_invoke — the gate lives in queue-append.

Converted from a hand-rolled unittest runner to collectable pytest functions.

Run: python3 -m pytest coordinator/bin/tests/test_lesson_add.py

Spec backlink: DoE-claude:pln-strang-08-arm-the-doe-queue-fa-36567b § C3
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import unittest.mock
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — locate CLI relative to this test file
# test file: coordinator/bin/tests/test_lesson_add.py
# CLI:       coordinator/bin/coordinator-lesson-add
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_CLI_PATH = _BIN_DIR / "coordinator-lesson-add.py"

# Load the CLI as a Python module for unit testing.
# The sh/python trampoline header (''''exec...) is inert when imported as Python.
_loader = importlib.machinery.SourceFileLoader("coordinator_lesson_add", str(_CLI_PATH))
_spec = importlib.util.spec_from_loader("coordinator_lesson_add", _loader)
_cli_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_cli_mod)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_ARGS = [
    "coordinator-lesson-add",          # sys.argv[0] — program name
    "--title", "Test lesson about foobar pattern usage",
    "--body", "Test lesson body prose",
    "--scope", "project",
]


def _invoke(*extra_args: str) -> int:
    """Invoke main() with patched sys.argv; return the exit code."""
    argv = list(_MINIMAL_ARGS) + list(extra_args)
    with unittest.mock.patch("sys.argv", argv):
        try:
            _cli_mod.main()
            return 0
        except SystemExit as exc:
            return int(exc.code) if exc.code is not None else 0


# ---------------------------------------------------------------------------
# AC3 — delegation: subprocess is invoked with coordinator-queue-append
#
# This is the native routing inheritance path: queue-append (C2-armed) routes
# through cc_invoke to queue.append when the seam is present. lesson-add
# inherits that routing automatically through its subprocess call — no gate
# of its own.
# ---------------------------------------------------------------------------


def test_subprocess_called_with_queue_append():
    """AC3: subprocess.run is called with coordinator-queue-append as the target."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0

    with (
        unittest.mock.patch.object(_cli_mod, "_dedup_check", return_value=[]),
        unittest.mock.patch("subprocess.run", return_value=mock_result) as mock_run,
    ):
        rc = _invoke()

    assert rc == 0
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    # cmd = [interpreter, _QUEUE_APPEND, "--schema", "lessons", ...]
    assert str(_cli_mod._QUEUE_APPEND) in cmd, (
        "coordinator-queue-append must be the subprocess delegation target"
    )


def test_subprocess_cmd_starts_with_interpreter():
    """AC3: delegation uses the current Python interpreter as cmd[0]."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0

    with (
        unittest.mock.patch.object(_cli_mod, "_dedup_check", return_value=[]),
        unittest.mock.patch("subprocess.run", return_value=mock_result) as mock_run,
    ):
        _invoke()

    cmd = mock_run.call_args[0][0]
    # Verify cmd[0] is a python interpreter path (same interpreter as the test runner).
    interpreter_path = cmd[0]
    assert "python" in interpreter_path.lower(), (
        "cmd[0] must be a Python interpreter — ensures subprocess runs queue-append as Python"
    )


def test_subprocess_cmd_includes_schema_lessons():
    """AC3: delegation passes --schema lessons to coordinator-queue-append."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0

    with (
        unittest.mock.patch.object(_cli_mod, "_dedup_check", return_value=[]),
        unittest.mock.patch("subprocess.run", return_value=mock_result) as mock_run,
    ):
        _invoke()

    cmd = mock_run.call_args[0][0]
    assert "--schema" in cmd, "--schema flag must be in the delegation command"
    schema_idx = cmd.index("--schema")
    assert cmd[schema_idx + 1] == "lessons", "--schema value must be 'lessons'"


def test_subprocess_cmd_passes_through_title_and_body():
    """AC3: --title and --body are forwarded to coordinator-queue-append."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0

    with (
        unittest.mock.patch.object(_cli_mod, "_dedup_check", return_value=[]),
        unittest.mock.patch("subprocess.run", return_value=mock_result) as mock_run,
    ):
        _invoke()

    cmd = mock_run.call_args[0][0]
    assert "--title" in cmd, "--title must be forwarded to queue-append"
    assert "--body" in cmd, "--body must be forwarded to queue-append"
    title_idx = cmd.index("--title")
    assert cmd[title_idx + 1] == "Test lesson about foobar pattern usage", (
        "title value must be forwarded verbatim"
    )


def test_exit_code_propagated_from_queue_append():
    """AC3: subprocess returncode from coordinator-queue-append is propagated."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 2

    with (
        unittest.mock.patch.object(_cli_mod, "_dedup_check", return_value=[]),
        unittest.mock.patch("subprocess.run", return_value=mock_result),
    ):
        rc = _invoke()

    assert rc == 2, "returncode from coordinator-queue-append must be propagated"


# ---------------------------------------------------------------------------
# AC3 — dedup: wrapper-layer pre-check fires and blocks delegation on duplicate
#
# queue.append (the native op) is write-always — no dedup at the op level.
# The wrapper-layer dedup check in lesson-add is therefore load-bearing and
# must not be removed or bypassed.
# ---------------------------------------------------------------------------


def _write_lesson_yaml(directory: str, title: str) -> str:
    """Write a minimal lesson YAML with the given title into directory."""
    path = os.path.join(directory, "2026-01-01T00-00-00Z-existing.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f'title: "{title}"\n')
        fh.write("body: existing lesson body\n")
    return path


def test_duplicate_rejected_before_delegation(tmp_path):
    """AC3: duplicate title exits nonzero and subprocess.run is NOT called."""
    tmpdir = str(tmp_path)
    # Existing lesson with identical title to _MINIMAL_ARGS --title
    _write_lesson_yaml(tmpdir, "Test lesson about foobar pattern usage")

    with (
        unittest.mock.patch.object(_cli_mod, "_lessons_dir", return_value=tmpdir),
        unittest.mock.patch("subprocess.run") as mock_run,
        unittest.mock.patch("sys.stderr", io.StringIO()),
    ):
        rc = _invoke()

    assert rc == 1, "duplicate title must exit 1"
    mock_run.assert_not_called()


def test_duplicate_emits_possible_duplicate_to_stderr(tmp_path):
    """AC3: dedup pre-check emits 'possible duplicate' message to stderr."""
    tmpdir = str(tmp_path)
    _write_lesson_yaml(tmpdir, "Test lesson about foobar pattern usage")

    captured_err = io.StringIO()
    with (
        unittest.mock.patch.object(_cli_mod, "_lessons_dir", return_value=tmpdir),
        unittest.mock.patch("subprocess.run"),
        unittest.mock.patch("sys.stderr", captured_err),
    ):
        _invoke()

    assert "possible duplicate" in captured_err.getvalue(), (
        "must emit 'possible duplicate' message when dedup match found"
    )


def test_force_flag_bypasses_dedup_and_delegates(tmp_path):
    """AC3: --force bypasses the dedup pre-check and proceeds to delegation."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0

    tmpdir = str(tmp_path)
    _write_lesson_yaml(tmpdir, "Test lesson about foobar pattern usage")

    with (
        unittest.mock.patch.object(_cli_mod, "_lessons_dir", return_value=tmpdir),
        unittest.mock.patch("subprocess.run", return_value=mock_result) as mock_run,
    ):
        rc = _invoke("--force")

    assert rc == 0, "--force must bypass dedup and succeed"
    mock_run.assert_called_once()


def test_no_duplicate_delegates_normally(tmp_path):
    """AC3: when no duplicate exists, delegation proceeds without obstruction."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0

    tmpdir = str(tmp_path)
    # Existing lesson with a completely different title — 0% overlap
    _write_lesson_yaml(tmpdir, "Totally different xyz topic abc 123")

    with (
        unittest.mock.patch.object(_cli_mod, "_lessons_dir", return_value=tmpdir),
        unittest.mock.patch("subprocess.run", return_value=mock_result) as mock_run,
    ):
        rc = _invoke()

    assert rc == 0
    mock_run.assert_called_once()


def test_below_threshold_no_match(tmp_path):
    """AC3 boundary: overlap below 60% threshold must not block delegation.

    New title tokens: {test, lesson, about, foobar, pattern, usage} (6 tokens).
    Existing title tokens: {test, lesson, about, routing, unique, words} (6 tokens).
    Intersection = {test, lesson, about} = 3; overlap = 3/6 = 0.50 < 0.60 → no match.
    Review: code-reviewer strang-08-slice3 — (F9/dispatch-F8) threshold boundary coverage.
    """
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0

    tmpdir = str(tmp_path)
    _write_lesson_yaml(tmpdir, "Test lesson about routing unique words")

    with (
        unittest.mock.patch.object(_cli_mod, "_lessons_dir", return_value=tmpdir),
        unittest.mock.patch("subprocess.run", return_value=mock_result) as mock_run,
    ):
        rc = _invoke()

    assert rc == 0, "below-threshold overlap must not block delegation"
    mock_run.assert_called_once()


def test_at_threshold_blocks_delegation(tmp_path):
    """AC3 boundary: overlap at or above 60% threshold must block delegation.

    New title tokens: {test, lesson, about, foobar, pattern, usage} (6 tokens).
    Existing title tokens: {test, lesson, about, foobar, pattern, other} (6 tokens).
    Intersection = {test, lesson, about, foobar, pattern} = 5; overlap = 5/6 ≈ 0.83 ≥ 0.60 → match.
    Review: code-reviewer strang-08-slice3 — (F9/dispatch-F8) threshold boundary coverage.
    """
    tmpdir = str(tmp_path)
    _write_lesson_yaml(tmpdir, "Test lesson about foobar pattern other")

    with (
        unittest.mock.patch.object(_cli_mod, "_lessons_dir", return_value=tmpdir),
        unittest.mock.patch("subprocess.run") as mock_run,
        unittest.mock.patch("sys.stderr", io.StringIO()),
    ):
        rc = _invoke()

    assert rc == 1, "at-threshold overlap must block delegation"
    mock_run.assert_not_called()


def test_empty_lessons_dir_delegates_normally(tmp_path):
    """AC3: empty lessons directory → no dedup match → delegation proceeds."""
    mock_result = unittest.mock.MagicMock()
    mock_result.returncode = 0

    tmpdir = str(tmp_path)
    with (
        unittest.mock.patch.object(_cli_mod, "_lessons_dir", return_value=tmpdir),
        unittest.mock.patch("subprocess.run", return_value=mock_result) as mock_run,
    ):
        rc = _invoke()

    assert rc == 0
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# _lessons_dir meta-repo routing — bug-backlog/2026-07-06-lesson-add-dedup-scans-wrong-dir
#
# _lessons_dir must route to <claude-klabauter>/state/lessons when cwd is the meta-repo.
#
# Bug: without this routing, cwd=~/.claude causes the dedup scan to look in
# ~/.claude/state/lessons while coordinator-queue-append writes to
# <claude-klabauter>/state/lessons — the dedup check silently misses existing entries.
#
# Fix: _lessons_dir aligns with queue-append._output_path else-branch
# (stop-the-rot meta-repo routing).
#
# Real-env/integration companion: test_coordinator_lesson_add_meta_routing.py
# drives this same branch through a real git repo + real env vars instead of
# mocking _git_root/_claude_home/_claude_klabauter_root — see that file's docstring for
# what it covers beyond this mocked suite.
#
# Spec backlink: state/bug-backlog/2026-07-06-lesson-add-dedup-scans-wrong-dir-in-meta.yaml
# ---------------------------------------------------------------------------


def test_meta_repo_routes_to_claude_klabauter():
    """When git root == ~/.claude, _lessons_dir returns <claude-klabauter>/state/lessons."""
    fake_home = "/fake/dot-claude"
    fake_claude_klabauter = "/fake/claude-klabauter"

    with (
        unittest.mock.patch.object(_cli_mod, "_git_root", return_value=fake_home),
        unittest.mock.patch.object(_cli_mod, "_claude_home", return_value=fake_home),
        unittest.mock.patch.object(_cli_mod, "_claude_klabauter_root", return_value=fake_claude_klabauter),
    ):
        result = _cli_mod._lessons_dir()

    expected = os.path.join(fake_claude_klabauter, "state", "lessons")
    assert result == expected, (
        f"meta-repo cwd must route dedup scan to claude-klabauter, got {result!r}"
    )


def test_sibling_repo_routes_to_git_root():
    """When git root != ~/.claude, _lessons_dir returns <git-root>/state/lessons."""
    fake_home = "/fake/dot-claude"
    fake_git_root = "/fake/sibling-repo"

    with (
        unittest.mock.patch.object(_cli_mod, "_git_root", return_value=fake_git_root),
        unittest.mock.patch.object(_cli_mod, "_claude_home", return_value=fake_home),
    ):
        result = _cli_mod._lessons_dir()

    expected = os.path.join(fake_git_root, "state", "lessons")
    assert result == expected, (
        f"sibling-repo cwd must route dedup scan to git root, got {result!r}"
    )


def test_meta_repo_unresolvable_claude_klabauter_falls_back():
    """When meta-repo and claude-klabauter is unresolvable, falls back to git-root path (degrades gracefully)."""
    fake_home = "/fake/dot-claude"
    captured_err = io.StringIO()

    with (
        unittest.mock.patch.object(_cli_mod, "_git_root", return_value=fake_home),
        unittest.mock.patch.object(_cli_mod, "_claude_home", return_value=fake_home),
        unittest.mock.patch.object(_cli_mod, "_claude_klabauter_root", return_value=None),
        unittest.mock.patch("sys.stderr", captured_err),
    ):
        result = _cli_mod._lessons_dir()

    # Fallback: git-root path (~/dot-claude/state/lessons)
    expected = os.path.join(fake_home, "state", "lessons")
    assert result == expected, (
        "unresolvable claude-klabauter must degrade gracefully to git-root path"
    )
    assert "claude-klabauter root unresolvable" in captured_err.getvalue(), (
        "unresolvable claude-klabauter must emit a warning to stderr"
    )


def test_env_override_takes_precedence_over_meta_repo_routing():
    """QUEUE_APPEND_OUTPUT_ROOT env var wins over meta-repo routing."""
    fake_home = "/fake/dot-claude"
    fake_override = "/fake/override-root"

    with (
        unittest.mock.patch.dict(
            os.environ, {_cli_mod._QUEUE_APPEND_OUTPUT_ROOT_ENV: fake_override}
        ),
        unittest.mock.patch.object(_cli_mod, "_git_root", return_value=fake_home),
        unittest.mock.patch.object(_cli_mod, "_claude_home", return_value=fake_home),
    ):
        result = _cli_mod._lessons_dir()

    expected = os.path.join(fake_override, "state", "lessons")
    assert result == expected, (
        "QUEUE_APPEND_OUTPUT_ROOT override must win over meta-repo routing"
    )


# ---------------------------------------------------------------------------
# AC6 — grep-gate: delegation structure intact; no independent routing gate
#
# The negative-spec assertion (no cc_invoke import) is the key invariant:
# lesson-add must NOT gain an independent arming site — native routing is
# inherited transitively through the subprocess delegation, not directly
# wired in lesson-add.
# ---------------------------------------------------------------------------


def _source() -> str:
    return _CLI_PATH.read_text(encoding="utf-8")


def test_queue_append_delegation_present():
    """Grep-gate: _QUEUE_APPEND constant and subprocess delegation are present."""
    source = _source()
    assert "_QUEUE_APPEND" in source, (
        "_QUEUE_APPEND must be present — it is the subprocess delegation target"
    )
    assert "subprocess.run(" in source, (
        "subprocess.run must be present for delegation to coordinator-queue-append"
    )


def test_dedup_check_call_present():
    """Grep-gate: _dedup_check() call is in main() — wrapper-layer dedup is load-bearing."""
    assert "_dedup_check(" in _source(), (
        "_dedup_check() call must be present in coordinator-lesson-add"
    )


def test_no_independent_routing_gate():
    """AC3/AC6: coordinator-lesson-add must NOT have an independent cc_invoke routing gate.

    Native routing is inherited transitively through the subprocess delegation to
    coordinator-queue-append. Adding a routing gate here would be a wrong second
    arming site — the gate lives in queue-append, not here.

    Narrowed (2026-08-15): this originally forbade any "cc_invoke" reference at
    all, which also caught `from cc_invoke import require_engine_on_path` — the
    sys.path bootstrap helper added deliberately in 9b979ee5f (2026-08-12) to
    fix a real critical-path failure on the mirror-resolved install (the CLI's
    dedup pre-check imports coordinator_core, which needs the engine root on
    sys.path first). That helper is the same bootstrap used by ~45 other
    coordinator/bin/ CLIs and is unrelated to routing — the actual routing
    gate this test guards against is `from cc_invoke import route`
    (coordinator-queue-append's own pattern, aliased `_cc_route`). Narrow the
    assertion to that specific import shape so the legitimate bootstrap import
    stays allowed while a real second arming site is still caught.
    """
    source = _source()
    assert "from cc_invoke import route" not in source, (
        "coordinator-lesson-add must not import cc_invoke.route — no independent routing gate"
    )
    assert "_cc_route" not in source, (
        "cc_invoke's route()/_cc_route seam must not be referenced in coordinator-lesson-add"
    )


def test_no_legacy_fn_defined():
    """AC3: coordinator-lesson-add defines no legacy_fn closure (no own write core).

    Unlike queue-append and lesson-promote, lesson-add has no write core of its own.
    The entire write is delegated to queue-append. There is no legacy_fn in lesson-add.
    """
    assert "def legacy_fn(" not in _source(), (
        "coordinator-lesson-add must not define legacy_fn — it has no own write core"
    )


def test_no_retired_transport():
    """AC9: coordinator-lesson-add must not reference retired transport patterns."""
    source = _source()
    for pattern in ("coordinator_core.client", "AF_UNIX", "auth_token", "three-state"):
        assert pattern not in source, (
            f"retired transport pattern '{pattern}' must not appear in coordinator-lesson-add"
        )

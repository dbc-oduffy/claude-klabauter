"""Pins for `coordinator/lib/percolate/assembled_mirror_gate.py`.

Three shapes must stay distinguishable, per the parent plan's own words
("A collection that errors and one that finds nothing must not read
alike"): a clean collection with N>0 tests, a clean collection with 0
tests (marker deselection / genuinely empty tree), and an ERRORED
collection (an unguarded import a module dropped from the tree). The
first two share `errored=False` and only differ in `collected_count`; the
third is `errored=True` regardless of `collected_count`. All three must
be `passed=False` except the first.

Also pins the sys.path-isolation contract (`_subprocess_env` strips
`PYTHONPATH`, `cwd` is the tree under test — never claude-klabauter's own root) via
a mocked `subprocess.run`, since asserting a negative ("nothing on
`sys.path` resolves to claude-klabauter") from a real child process is not
observable from the test process directly.

Spec: docs/plans/2026-08-28-a-dropped-module-must-not-leave-its-test-behind.md
chunk C2.
"""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# `coordinator/` and `coordinator/lib/` carry no `__init__.py`; `coordinator/
# lib/percolate/` does. Same sys.path convention the sibling tests in this
# directory already use (see test_import_closure_depth.py).
_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate.assembled_mirror_gate import (  # noqa: E402
    MARKER_EXPRESSION,
    _parse_collection_summary,
    _subprocess_env,
    format_refusal,
    run_assembled_mirror_gate,
)


_PYPROJECT = """\
[tool.pytest.ini_options]
testpaths = ["."]
markers = [
    "cadence: heavy suite, runs at cadence gates, not per-commit",
    "pending_fix: known-broken path assumption",
    "designed_red: red by design",
]
"""


def _write_tree(tmp_path: Path, test_source: "str | None") -> Path:
    tree = tmp_path / "assembled_mirror"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    if test_source is not None:
        (tree / "test_probe.py").write_text(test_source, encoding="utf-8")
    return tree


# --- real subprocess runs: the three shapes -------------------------------


def test_healthy_tree_passes_and_reports_the_real_count(tmp_path):
    tree = _write_tree(
        tmp_path,
        "def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n",
    )
    result = run_assembled_mirror_gate(tree, timeout_s=30.0)
    assert result.passed is True
    assert result.errored is False
    assert result.collected_count == 2
    assert result.exit_code == 0


def test_dropped_module_orphan_import_errors_collection_and_refuses(tmp_path):
    """THE plan's own shape: a test reaches for a module the tree never
    shipped, unguarded, at module scope — collection is interrupted, not
    merely one red test."""
    tree = _write_tree(
        tmp_path,
        "from coordinator_core.benchmarks.this_module_was_dropped import thing\n\n\n"
        "def test_uses_it():\n    assert thing\n",
    )
    result = run_assembled_mirror_gate(tree, timeout_s=30.0)
    assert result.passed is False
    assert result.errored is True
    assert result.collected_count == 0


def test_zero_collected_reads_differently_from_an_error(tmp_path):
    """Marker deselection to zero (or a genuinely empty tree) refuses like
    an error does (any non-zero exit refuses — parent plan body), but MUST
    be reported as a distinct shape: `errored=False`, not `errored=True`."""
    tree = _write_tree(
        tmp_path,
        "import pytest\n\n\n@pytest.mark.cadence\n"
        "def test_only_the_deselected_marker():\n    assert True\n",
    )
    result = run_assembled_mirror_gate(tree, timeout_s=30.0)
    assert result.passed is False
    assert result.errored is False
    assert result.collected_count == 0


def test_the_two_zero_shapes_are_never_conflated(tmp_path):
    errored_tree = tmp_path / "errored_tree"
    errored_tree.mkdir()
    (errored_tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (errored_tree / "test_probe.py").write_text(
        "import this_module_does_not_exist_anywhere_1234\n", encoding="utf-8"
    )
    clean_zero_tree = tmp_path / "clean_zero_tree"
    clean_zero_tree.mkdir()
    (clean_zero_tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")

    errored_result = run_assembled_mirror_gate(errored_tree, timeout_s=30.0)
    clean_zero_result = run_assembled_mirror_gate(clean_zero_tree, timeout_s=30.0)

    assert errored_result.passed is False
    assert clean_zero_result.passed is False
    assert errored_result.collected_count == clean_zero_result.collected_count == 0
    assert errored_result.errored is True
    assert clean_zero_result.errored is False
    assert errored_result.errored != clean_zero_result.errored


# --- refusal message reports the denominator -------------------------------


def test_format_refusal_names_the_collected_count_and_shape():
    tree = str(Path("dummy") / "tree")
    from percolate.assembled_mirror_gate import MirrorCollectionResult

    errored = MirrorCollectionResult(
        passed=False,
        collected_count=0,
        errored=True,
        exit_code=2,
        timed_out=False,
        elapsed_s=1.23,
        command=("python", "-m", "pytest"),
        tree_root=tree,
        stdout_tail="",
        stderr_tail="",
    )
    msg = format_refusal(errored)
    assert "ERRORED" in msg
    assert "0 test(s) collected" in msg
    assert "exit=2" in msg

    clean_zero = MirrorCollectionResult(
        passed=False,
        collected_count=0,
        errored=False,
        exit_code=5,
        timed_out=False,
        elapsed_s=0.5,
        command=("python", "-m", "pytest"),
        tree_root=tree,
        stdout_tail="",
        stderr_tail="",
    )
    msg2 = format_refusal(clean_zero)
    assert "found 0 test(s)" in msg2
    assert msg != msg2


# --- sys.path isolation: cannot reach claude-klabauter --------------------------------


def test_subprocess_isolated_from_claude_klabauter_via_cwd_and_stripped_pythonpath(tmp_path, monkeypatch):
    """Cannot observe an empty sys.path from a real child process
    end-to-end without shipping a full fake tree; pins the two channels
    this module actually controls instead: `cwd` is the tree under test
    (never claude-klabauter's own root, never an ancestor of it), and `PYTHONPATH`
    is stripped from the child's env so nothing claude-klabauter put there leaks in."""
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[4]))
    tree = tmp_path / "isolated_tree"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")

    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "0 tests collected in 0.01s\n"
        stderr = ""

    def _fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return _FakeCompleted()

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run", _fake_run):
        run_assembled_mirror_gate(tree, timeout_s=5.0)

    assert captured["cwd"] == str(tree)
    assert "PYTHONPATH" not in captured["env"]
    claude_klabauter_root = str(Path(__file__).resolve().parents[4])
    assert claude_klabauter_root not in captured["cwd"]


def test_command_uses_the_trees_own_documented_marker_expression():
    assert MARKER_EXPRESSION == "not cadence and not pending_fix and not designed_red"


# --- summary-parsing unit pins ----------------------------------------------


@pytest.mark.parametrize(
    "stdout,expected_count,expected_errored",
    [
        ("test_a.py::test_one\ntest_a.py::test_two\n\n2 tests collected in 0.02s\n", 2, False),
        ("3/12 tests collected (9 deselected) in 0.04s\n", 3, False),
        # A partial collection: pytest reports the count it DID reach and its
        # own error tally on the same line. The count is the denominator for
        # those errors, never evidence against them -- read as a clean
        # collection, this shape made the gate refuse a publish in the same
        # sentence that called the tree clean.
        (
            "22938/39613 tests collected (16675 deselected), 5 errors in 11.20s\n",
            22938,
            True,
        ),
        ("7 tests collected, 1 error in 0.30s\n", 7, True),
        # "error" inside a collected test id is prose, not a tally.
        ("test_a.py::test_error_handling\n\n1 test collected in 0.02s\n", 1, False),
        ("no tests ran in 0.01s\n", 0, False),
        ("no tests collected in 0.01s\n", 0, False),
        (
            "ERROR test_probe.py - ModuleNotFoundError: No module named 'x'\n"
            "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!\n",
            0,
            True,
        ),
        ("", 0, True),
        ("some totally unrecognised summary shape\n", 0, True),
    ],
)
def test_parse_collection_summary_shapes(stdout, expected_count, expected_errored):
    count, errored = _parse_collection_summary(stdout)
    assert count == expected_count
    assert errored == expected_errored

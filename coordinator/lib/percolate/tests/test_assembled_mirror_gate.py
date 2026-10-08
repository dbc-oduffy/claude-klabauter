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

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate.assembled_mirror_gate import (  # noqa: E402
    MARKER_EXPRESSION,
    _PYTEST_ENV_SCRUB,
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
    (tree / "coordinator_core").mkdir()
    if test_source is not None:
        (tree / "test_probe.py").write_text(test_source, encoding="utf-8")
    return tree


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
    (errored_tree / "coordinator_core").mkdir()
    (errored_tree / "test_probe.py").write_text(
        "import this_module_does_not_exist_anywhere_1234\n", encoding="utf-8"
    )
    clean_zero_tree = tmp_path / "clean_zero_tree"
    clean_zero_tree.mkdir()
    (clean_zero_tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (clean_zero_tree / "coordinator_core").mkdir()

    errored_result = run_assembled_mirror_gate(errored_tree, timeout_s=30.0)
    clean_zero_result = run_assembled_mirror_gate(clean_zero_tree, timeout_s=30.0)

    assert errored_result.passed is False
    assert clean_zero_result.passed is False
    assert errored_result.collected_count == clean_zero_result.collected_count == 0
    assert errored_result.errored is True
    assert clean_zero_result.errored is False
    assert errored_result.errored != clean_zero_result.errored


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
        verdict_obtained=True,
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
        verdict_obtained=True,
    )
    msg2 = format_refusal(clean_zero)
    assert "found 0 test(s)" in msg2
    assert msg != msg2


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
    (tree / "coordinator_core").mkdir()

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


def test_command_uses_the_trees_own_documented_marker_expression():
    assert MARKER_EXPRESSION == "not cadence and not pending_fix and not designed_red"


def test_tree_missing_coordinator_core_refuses_before_running_a_subprocess(tmp_path):
    tree = tmp_path / "no_coordinator_core"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run") as run_mock:
        result = run_assembled_mirror_gate(tree, timeout_s=5.0)

    run_mock.assert_not_called()
    assert result.passed is False
    assert result.isolation_unverified is True
    assert result.errored is True
    assert result.exit_code is None
    assert "coordinator_core" in format_refusal(result)


def test_tree_missing_coordinator_core_and_not_in_declared_scope_is_not_applicable(tmp_path):
    tree = tmp_path / "no_coordinator_core_declared_absent"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run") as run_mock:
        result = run_assembled_mirror_gate(
            tree, timeout_s=5.0, coordinator_core_in_declared_scope=False
        )

    run_mock.assert_not_called()
    assert result.not_applicable is True
    assert result.isolation_unverified is False
    assert result.passed is False
    assert result.exit_code is None
    assert result.is_incomplete is False


def test_tree_missing_coordinator_core_but_declared_in_scope_still_refuses(tmp_path):
    tree = tmp_path / "no_coordinator_core_declared_present"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run") as run_mock:
        result = run_assembled_mirror_gate(
            tree, timeout_s=5.0, coordinator_core_in_declared_scope=True
        )

    run_mock.assert_not_called()
    assert result.not_applicable is False
    assert result.isolation_unverified is True
    assert result.passed is False


def test_coordinator_core_in_declared_scope_defaults_true_never_silently_not_applicable(tmp_path):
    tree = tmp_path / "no_coordinator_core_default"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run") as run_mock:
        result = run_assembled_mirror_gate(tree, timeout_s=5.0)

    run_mock.assert_not_called()
    assert result.not_applicable is False
    assert result.isolation_unverified is True


def test_tree_carrying_coordinator_core_ignores_declared_scope_flag(tmp_path):
    tree = _write_tree(
        tmp_path,
        "def test_one():\n    assert True\n",
    )
    result = run_assembled_mirror_gate(
        tree, timeout_s=30.0, coordinator_core_in_declared_scope=False
    )
    assert result.not_applicable is False
    assert result.isolation_unverified is False
    assert result.passed is True


# --- CONTENT vs INCOMPLETE classification -----------------------------------


def test_timed_out_result_is_incomplete_and_carries_no_content_verdict(tmp_path):
    tree = tmp_path / "slow_tree"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tree / "coordinator_core").mkdir()

    import subprocess as _subprocess

    def _fake_run(command, **kwargs):
        raise _subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run", _fake_run):
        result = run_assembled_mirror_gate(tree, timeout_s=0.01)

    assert result.timed_out is True
    assert result.is_incomplete is True
    assert result.passed is False
    assert result.timeout_s == 0.01


def test_completed_run_carries_a_content_verdict_never_the_incomplete_one(tmp_path):
    tree = _write_tree(
        tmp_path,
        "def test_one():\n    assert True\n",
    )
    result = run_assembled_mirror_gate(tree, timeout_s=30.0)
    assert result.is_incomplete is False
    assert result.timed_out is False


def test_errored_completed_collection_is_a_content_verdict_not_incomplete(tmp_path):
    tree = _write_tree(
        tmp_path,
        "from coordinator_core.benchmarks.this_module_was_dropped import thing\n\n\n"
        "def test_uses_it():\n    assert thing\n",
    )
    result = run_assembled_mirror_gate(tree, timeout_s=30.0)
    assert result.errored is True
    assert result.is_incomplete is False


def test_isolation_unverified_result_is_incomplete(tmp_path):
    tree = tmp_path / "no_coordinator_core_2"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run") as run_mock:
        result = run_assembled_mirror_gate(tree, timeout_s=42.0)

    run_mock.assert_not_called()
    assert result.is_incomplete is True
    assert result.timeout_s == 42.0


def test_omitting_verdict_obtained_reads_incomplete_never_clean():
    """The default's DIRECTION, pinned. A construction site that never
    heard of `verdict_obtained` must refuse, not assert a content verdict:
    with a True default, omission claims a verdict was obtained, and a
    declared exemption may then waive it -- the exact shape widening
    `is_incomplete` exists to close, reintroduced through a default. This
    test fails if anyone flips the default back for backwards
    compatibility with older fixtures."""
    from percolate.assembled_mirror_gate import MirrorCollectionResult

    forgetful = MirrorCollectionResult(
        passed=False,
        collected_count=12,
        errored=False,
        exit_code=0,
        timed_out=False,
        elapsed_s=1.0,
        command=("python", "-m", "pytest"),
        tree_root="tree",
        stdout_tail="",
        stderr_tail="",
    )
    assert forgetful.is_incomplete is True


def test_non_default_timeout_s_is_reported_in_both_timed_out_and_incomplete_renderings(tmp_path):
    tree = tmp_path / "slow_tree_2"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tree / "coordinator_core").mkdir()

    import subprocess as _subprocess

    def _fake_run(command, **kwargs):
        raise _subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run", _fake_run):
        timed_out_result = run_assembled_mirror_gate(tree, timeout_s=7.5)

    isolation_tree = tmp_path / "no_coordinator_core_3"
    isolation_tree.mkdir()
    (isolation_tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    isolation_result = run_assembled_mirror_gate(isolation_tree, timeout_s=7.5)

    timed_out_msg = format_refusal(timed_out_result)
    isolation_msg = format_refusal(isolation_result)
    assert "TIMED OUT" in timed_out_msg
    assert "budget 8s" in timed_out_msg
    assert "INCOMPLETE" in timed_out_msg
    assert "INCOMPLETE" in isolation_msg
    assert "budget 8s" in isolation_msg


@pytest.mark.parametrize(
    "stdout,expected_count,expected_errored,expected_recognized",
    [
        (
            "test_a.py::test_one\ntest_a.py::test_two\n\n2 tests collected in 0.02s\n",
            2,
            False,
            True,
        ),
        ("3/12 tests collected (9 deselected) in 0.04s\n", 3, False, True),
        (
            "22938/39613 tests collected (16675 deselected), 5 errors in 11.20s\n",
            22938,
            True,
            True,
        ),
        ("7 tests collected, 1 error in 0.30s\n", 7, True, True),
        (
            "test_a.py::test_error_handling\n\n1 test collected in 0.02s\n",
            1,
            False,
            True,
        ),
        ("no tests ran in 0.01s\n", 0, False, True),
        ("no tests collected in 0.01s\n", 0, False, True),
        (
            "ERROR test_probe.py - ModuleNotFoundError: No module named 'x'\n"
            "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!\n",
            0,
            True,
            True,
        ),
        ("", 0, True, False),
        ("some totally unrecognised summary shape\n", 0, True, False),
    ],
)
def test_parse_collection_summary_shapes(stdout, expected_count, expected_errored, expected_recognized):
    count, errored, recognized = _parse_collection_summary(stdout)
    assert count == expected_count
    assert errored == expected_errored
    assert recognized == expected_recognized


def test_empty_stdout_is_incomplete_with_no_content_claim(tmp_path):
    """A pytest child killed before it could write ANY summary (memory
    pressure, an OS/job-object kill, a plugin segfault) must read as
    INCOMPLETE, never as a clean or errored CONTENT verdict about the
    tree -- this is the exact hole `timed_out or isolation_unverified`
    missed."""
    tree = tmp_path / "empty_stdout_tree"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tree / "coordinator_core").mkdir()

    class _FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = ""

    def _fake_run(command, **kwargs):
        return _FakeCompleted()

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run", _fake_run):
        result = run_assembled_mirror_gate(tree, timeout_s=5.0)

    assert result.is_incomplete is True
    assert result.passed is False
    assert result.timed_out is False
    assert result.isolation_unverified is False


def test_unrecognised_summary_is_incomplete_with_no_content_claim(tmp_path):
    tree = tmp_path / "unrecognised_tree"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tree / "coordinator_core").mkdir()

    class _FakeCompleted:
        returncode = 1
        stdout = "some totally unrecognised summary shape\n"
        stderr = ""

    def _fake_run(command, **kwargs):
        return _FakeCompleted()

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run", _fake_run):
        result = run_assembled_mirror_gate(tree, timeout_s=5.0)

    assert result.is_incomplete is True
    assert result.passed is False


def test_negative_returncode_signal_death_is_incomplete_even_with_a_parseable_tail(tmp_path):
    tree = tmp_path / "signal_killed_tree"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tree / "coordinator_core").mkdir()

    class _FakeCompleted:
        returncode = -9
        stdout = "3 tests collected in 0.02s\n"
        stderr = ""

    def _fake_run(command, **kwargs):
        return _FakeCompleted()

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run", _fake_run):
        result = run_assembled_mirror_gate(tree, timeout_s=5.0)

    assert result.is_incomplete is True
    assert result.passed is False


def test_spawn_oserror_returns_incomplete_rather_than_raising(tmp_path):
    tree = tmp_path / "oserror_tree"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tree / "coordinator_core").mkdir()

    def _fake_run(command, **kwargs):
        raise OSError("could not create process: resource temporarily unavailable")

    with mock.patch("percolate.assembled_mirror_gate.subprocess.run", _fake_run):
        result = run_assembled_mirror_gate(tree, timeout_s=5.0)

    assert result.passed is False
    assert result.is_incomplete is True
    assert result.exit_code is None


def test_scrubbed_env_omits_the_nested_pytest_env_vars(tmp_path, monkeypatch):
    """A peer's `PYTEST_ADDOPTS` (e.g. `-n auto`) must not reach this
    module's own nested pytest child -- it is reused, not reinvented, from
    `_NESTED_PYTEST_ENV_SCRUB` in
    coordinator/bin/tests/test_zero_test_module_ratchet.py."""
    for var in _PYTEST_ENV_SCRUB:
        monkeypatch.setenv(var, "some-inherited-value")

    env = _subprocess_env()
    for var in _PYTEST_ENV_SCRUB:
        assert var not in env


def test_default_timeout_is_a_hang_guard_not_a_speed_bar():
    """REGRESSION PIN, 2026-09-01. `DEFAULT_TIMEOUT_S` was 60.0 against a
    collection measured at 23.97s of work but 32s-60.3s of WALL CLOCK
    depending on how many of the box's ~50 sessions were running. The bar
    therefore decided, by coin flip, whether every publish on the fleet came
    back "unverified" -- a wall-clock measurement standing in for a claim
    about the tree, which this repo's load norm forbids outright.

    This pins the INTENT, not a magic number: the default must stay far
    enough above the measured collection cost that peer load cannot reach
    it. Tightening it back toward the measured cost reinstates the coin
    flip, so that change should fail here and be argued rather than tuned.
    """
    from percolate.assembled_mirror_gate import DEFAULT_TIMEOUT_S

    measured_collection_seconds = 24.0
    assert DEFAULT_TIMEOUT_S >= measured_collection_seconds * 8, (
        "DEFAULT_TIMEOUT_S is close enough to real collection cost that peer "
        "load alone can trip it -- see the constant's own negative spec"
    )


def test_a_timed_out_gate_still_makes_no_claim_about_the_tree(tmp_path):
    """The budget change must not touch the hard-won half: a run that could
    not finish reports INCOMPLETE, never a pass and never a content-bearing
    fail. Pinned here beside the budget so the two are read together -- the
    number is safe to move only while this stays true."""
    from percolate.assembled_mirror_gate import (
        DEFAULT_TIMEOUT_S,
        MirrorCollectionResult,
        format_refusal,
    )

    result = MirrorCollectionResult(
        passed=False,
        errored=True,
        timed_out=True,
        collected_count=0,
        exit_code=None,
        elapsed_s=DEFAULT_TIMEOUT_S,
        timeout_s=DEFAULT_TIMEOUT_S,
        command=("python", "-m", "pytest"),
        tree_root=tmp_path,
        stdout_tail="",
        stderr_tail="",
    )
    assert result.is_incomplete is True
    assert "INCOMPLETE" in format_refusal(result)

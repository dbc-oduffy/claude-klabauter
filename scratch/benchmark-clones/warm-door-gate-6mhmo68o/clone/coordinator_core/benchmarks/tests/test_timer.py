"""Unit tests for coordinator_core.benchmarks.timer.time_invocation() (AC9 guard).

Covers AC9: a mocked subprocess returning a non-zero exit code is REJECTED
(time_invocation raises BenchmarkSampleInvalid, never times/records the
sample), and a mocked exit-0 whose stdout is a JSON-RPC error envelope
({"error": ...}) is likewise REJECTED. All subprocess calls are mocked via
unittest.mock -- no real child processes are spawned here.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C8 (AC9).
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

import subprocess

from coordinator_core.benchmarks.timer import (
    SUBPROCESS_TIMEOUT_S,
    BenchmarkSampleInvalid,
    time_invocation,
)


def _completed(returncode: int, stdout: str):
    completed = mock.Mock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = ""
    return completed


@mock.patch("coordinator_core.benchmarks.timer.subprocess.run")
def test_exit_code_1_is_rejected_even_with_success_looking_stdout(mock_run):
    """AC9: non-zero exit code invalidates the sample regardless of stdout
    content -- must raise, never return a timing."""
    mock_run.return_value = _completed(
        1, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    )

    with pytest.raises(BenchmarkSampleInvalid) as excinfo:
        time_invocation("ping", "{}", repo=None)

    assert excinfo.value.op == "ping"
    assert excinfo.value.returncode == 1
    mock_run.assert_called_once()


@mock.patch("coordinator_core.benchmarks.timer.subprocess.run")
def test_exit_0_with_error_envelope_is_rejected(mock_run):
    """AC9: exit code 0 is necessary but not sufficient -- a JSON-RPC error
    envelope in stdout must also invalidate the sample."""
    mock_run.return_value = _completed(
        0,
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no such op"}}
        ),
    )

    with pytest.raises(BenchmarkSampleInvalid) as excinfo:
        time_invocation("coverage.gate", "{}", repo="/tmp/fixture-repo")

    assert excinfo.value.op == "coverage.gate"
    assert excinfo.value.returncode == 0
    assert "error" in excinfo.value.stdout_excerpt


@mock.patch("coordinator_core.benchmarks.timer.subprocess.run")
def test_exit_0_with_result_envelope_is_accepted_and_timed(mock_run):
    """The valid-sample control case: exit 0 + a 'result' envelope must
    return a float elapsed_ms and must not raise."""
    mock_run.return_value = _completed(
        0, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    )

    elapsed_ms = time_invocation("ping", "{}", repo=None)

    assert isinstance(elapsed_ms, float)
    assert elapsed_ms >= 0.0
    mock_run.assert_called_once()


@mock.patch("coordinator_core.benchmarks.timer.subprocess.run")
def test_exit_0_with_unparsable_stdout_is_rejected(mock_run):
    """Unparsable stdout on a healthy-looking exit 0 is itself invalid --
    a real invoke entrypoint always emits parsable JSON-RPC on success."""
    mock_run.return_value = _completed(0, "not json at all")

    with pytest.raises(BenchmarkSampleInvalid):
        time_invocation("ping", "{}", repo=None)


@mock.patch("coordinator_core.benchmarks.timer.subprocess.run")
def test_invalid_sample_never_reaches_a_timing_return(mock_run):
    """Explicitly assert the AC9 contract's second half: an invalid sample
    does not merely raise eventually -- it raises BEFORE any value is
    returned to the caller (no partial/garbage timing leaks out)."""
    mock_run.return_value = _completed(
        1, json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "boom"}})
    )

    result = None
    try:
        result = time_invocation("ping", "{}", repo=None)
    except BenchmarkSampleInvalid:
        # Expected per the AC9 contract under test -- the assertion below is the check.
        pass

    assert result is None


@mock.patch("coordinator_core.benchmarks.timer.subprocess.run")
def test_repo_flag_passed_through_for_worktree_scoped_op(mock_run):
    """Sanity check that the mocked call receives the --repo argv shape for
    a worktree-scoped op -- not an AC9 assertion, but guards the fixture
    setup used by the AC9 tests above."""
    mock_run.return_value = _completed(
        0, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
    )

    time_invocation("coverage.gate", "{}", repo="/tmp/fixture-repo")

    called_argv = mock_run.call_args[0][0]
    assert "--repo" in called_argv
    assert "/tmp/fixture-repo" in called_argv


@mock.patch("coordinator_core.benchmarks.timer.subprocess.run")
def test_exit_0_with_non_dict_json_body_is_rejected(mock_run):
    """Review: code-reviewer (Slice C F2, nit) -- a parsable-but-non-dict JSON
    body (a bare list or scalar) on exit 0 is not a valid JSON-RPC envelope
    either; only a dict without an 'error' key is accepted. Covers both the
    list and scalar shapes."""
    mock_run.return_value = _completed(0, json.dumps([1, 2, 3]))

    with pytest.raises(BenchmarkSampleInvalid):
        time_invocation("ping", "{}", repo=None)


@mock.patch("coordinator_core.benchmarks.timer.subprocess.run")
def test_exit_0_with_scalar_json_body_is_rejected(mock_run):
    mock_run.return_value = _completed(0, json.dumps("ok"))

    with pytest.raises(BenchmarkSampleInvalid):
        time_invocation("ping", "{}", repo=None)


@mock.patch("coordinator_core.benchmarks.timer.subprocess.run")
def test_subprocess_timeout_is_rejected_as_invalid_sample(mock_run):
    """Review: code-reviewer (Slice B F3, P2) -- a hung child process must
    fail loud like any other invalid sample, not wedge the run forever."""
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["invoke"], timeout=SUBPROCESS_TIMEOUT_S)

    with pytest.raises(BenchmarkSampleInvalid):
        time_invocation("ping", "{}", repo=None)


@mock.patch("coordinator_core.benchmarks.timer.subprocess.run")
def test_repo_flag_omitted_for_bare_scoped_op(mock_run):
    mock_run.return_value = _completed(
        0, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
    )

    time_invocation("ping", "{}", repo=None)

    called_argv = mock_run.call_args[0][0]
    assert "--repo" not in called_argv

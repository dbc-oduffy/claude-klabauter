"""
coordinator_core.ops.test_run_semgrep_scan

Characterization tests for the "ci.run_semgrep_scan" op
(coordinator_core.ops.run_semgrep_scan) — the diff-scoped semgrep wrapper replacing
the coordinator-claude security-audit-worker fence.

Coverage:
  (a) registered under exactly "ci.run_semgrep_scan" on import
  (b) missing repo_root / missing diff_base each raise a descriptive ValueError
  (c) empty diff scope (no changed files) -> clean skip, tier_used="empty_scope",
      semgrep never invoked (mocked subprocess.run asserts zero calls)
  (d) semgrep absent from PATH -> tier_used="unavailable", empty findings (mocked
      shutil.which; no real semgrep install required to pass this suite)
  (e) happy path: mocked semgrep --json output is mapped to findings +
      severity_counts, tier_used="semgrep"
  (f) unresolvable diff_base (git diff exits non-zero) -> ValueError naming the ref
  (g) semgrep genuine error (exit code outside {0,1}) -> ValueError, not swallowed
  (h) idempotency (AC7): two invocations with identical params + unchanged tree
      state produce byte-identical results

Spec backlink: pln-coordinator-ops-buildout-from--903224 § Wave 2
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.run_semgrep_scan  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.run_semgrep_scan import _run_semgrep_scan

_OP_NAME = "ci.run_semgrep_scan"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.run_semgrep_scan @register_op did not fire"
)


def _git_diff_result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git", "diff"], returncode=returncode, stdout=stdout, stderr=""
    )


def _semgrep_result(payload: dict, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["semgrep"], returncode=returncode, stdout=json.dumps(payload), stderr=""
    )


def test_registered_under_exact_op_key():
    assert _OP_NAME in _REGISTRY


def test_missing_repo_root_raises(tmp_path):
    with pytest.raises(ValueError, match="repo_root is None"):
        _run_semgrep_scan({"diff_base": "main"}, repo_root=None)


def test_missing_diff_base_raises(tmp_path):
    with pytest.raises(ValueError, match="diff_base"):
        _run_semgrep_scan({}, repo_root=tmp_path)


def test_blank_diff_base_raises(tmp_path):
    with pytest.raises(ValueError, match="diff_base"):
        _run_semgrep_scan({"diff_base": "   "}, repo_root=tmp_path)


def test_empty_diff_scope_skips_semgrep(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _git_diff_result(stdout="")
        result = _run_semgrep_scan({"diff_base": "main"}, repo_root=tmp_path)

    assert result == {"findings": [], "tier_used": "empty_scope", "severity_counts": {}}
    # Only the git-diff call happened — semgrep was never invoked.
    assert mock_run.call_count == 1
    assert mock_run.call_args[0][0][0] == "git"


def test_semgrep_unavailable_falls_back(tmp_path):
    changed = tmp_path / "changed.py"
    changed.write_text("x = 1\n")

    with patch("subprocess.run") as mock_run, patch(
        "coordinator_core.ops.run_semgrep_scan.shutil.which", return_value=None
    ):
        mock_run.return_value = _git_diff_result(stdout="changed.py\n")
        result = _run_semgrep_scan({"diff_base": "main"}, repo_root=tmp_path)

    assert result == {"findings": [], "tier_used": "unavailable", "severity_counts": {}}
    # git diff ran; semgrep was never invoked (only one subprocess.run call).
    assert mock_run.call_count == 1


def test_happy_path_maps_findings_and_severity_counts(tmp_path):
    changed = tmp_path / "changed.py"
    changed.write_text("x = 1\n")

    semgrep_payload = {
        "results": [
            {
                "check_id": "python.lang.security.eval",
                "path": "changed.py",
                "start": {"line": 1},
                "end": {"line": 1},
                "extra": {"severity": "ERROR", "message": "eval is dangerous"},
            },
            {
                "check_id": "python.lang.best-practice.unused",
                "path": "changed.py",
                "start": {"line": 2},
                "end": {"line": 2},
                "extra": {"severity": "WARNING", "message": "unused var"},
            },
        ]
    }

    with patch("subprocess.run") as mock_run, patch(
        "coordinator_core.ops.run_semgrep_scan.shutil.which", return_value="/usr/bin/semgrep"
    ):
        mock_run.side_effect = [
            _git_diff_result(stdout="changed.py\n"),
            _semgrep_result(semgrep_payload, returncode=1),
        ]
        result = _run_semgrep_scan(
            {"diff_base": "main", "config": "r/python"}, repo_root=tmp_path
        )

    assert result["tier_used"] == "semgrep"
    assert result["severity_counts"] == {"ERROR": 1, "WARNING": 1}
    assert len(result["findings"]) == 2
    assert result["findings"][0]["check_id"] == "python.lang.security.eval"
    assert result["findings"][0]["start_line"] == 1

    # config was forwarded verbatim as --config=<value>
    semgrep_call_args = mock_run.call_args_list[1][0][0]
    assert "--config=r/python" in semgrep_call_args
    assert "--json" in semgrep_call_args


def test_default_config_is_auto(tmp_path):
    changed = tmp_path / "changed.py"
    changed.write_text("x = 1\n")

    with patch("subprocess.run") as mock_run, patch(
        "coordinator_core.ops.run_semgrep_scan.shutil.which", return_value="/usr/bin/semgrep"
    ):
        mock_run.side_effect = [
            _git_diff_result(stdout="changed.py\n"),
            _semgrep_result({"results": []}, returncode=0),
        ]
        _run_semgrep_scan({"diff_base": "main"}, repo_root=tmp_path)

    semgrep_call_args = mock_run.call_args_list[1][0][0]
    assert "--config=auto" in semgrep_call_args


def test_unresolvable_diff_base_raises(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _git_diff_result(
            stdout="", returncode=128
        )
        with pytest.raises(ValueError, match="git diff"):
            _run_semgrep_scan({"diff_base": "not-a-real-ref"}, repo_root=tmp_path)


def test_semgrep_genuine_error_raises(tmp_path):
    changed = tmp_path / "changed.py"
    changed.write_text("x = 1\n")

    with patch("subprocess.run") as mock_run, patch(
        "coordinator_core.ops.run_semgrep_scan.shutil.which", return_value="/usr/bin/semgrep"
    ):
        mock_run.side_effect = [
            _git_diff_result(stdout="changed.py\n"),
            subprocess.CompletedProcess(
                args=["semgrep"], returncode=2, stdout="", stderr="bad config"
            ),
        ]
        with pytest.raises(ValueError, match="semgrep exited"):
            _run_semgrep_scan({"diff_base": "main"}, repo_root=tmp_path)


def test_git_diff_and_semgrep_calls_carry_timeout(tmp_path):
    """Review: code-reviewer (F2, P1) — neither subprocess.run call carried
    a timeout; a stuck git/semgrep invocation would wedge this op's worker
    thread forever."""
    changed = tmp_path / "changed.py"
    changed.write_text("x = 1\n")

    with patch("subprocess.run") as mock_run, patch(
        "coordinator_core.ops.run_semgrep_scan.shutil.which", return_value="/usr/bin/semgrep"
    ):
        mock_run.side_effect = [
            _git_diff_result(stdout="changed.py\n"),
            _semgrep_result({"results": []}, returncode=0),
        ]
        _run_semgrep_scan({"diff_base": "main"}, repo_root=tmp_path)

    git_kwargs = mock_run.call_args_list[0][1]
    semgrep_kwargs = mock_run.call_args_list[1][1]
    assert git_kwargs.get("timeout")
    assert semgrep_kwargs.get("timeout")


def test_git_diff_timeout_raises_value_error(tmp_path):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["git", "diff"], timeout=30)
        with pytest.raises(ValueError, match="timed out"):
            _run_semgrep_scan({"diff_base": "main"}, repo_root=tmp_path)


def test_diff_base_leading_dash_rejected(tmp_path):
    """Review: code-reviewer (F5, nit) — diff_base is passed positionally
    to `git diff` with no `--` separator; a value starting with '-' would
    be misparsed as a git option."""
    with pytest.raises(ValueError, match="looks like a git option"):
        _run_semgrep_scan({"diff_base": "-x"}, repo_root=tmp_path)


def test_idempotent_double_invocation(tmp_path):
    changed = tmp_path / "changed.py"
    changed.write_text("x = 1\n")

    semgrep_payload = {
        "results": [
            {
                "check_id": "python.lang.security.eval",
                "path": "changed.py",
                "start": {"line": 1},
                "end": {"line": 1},
                "extra": {"severity": "ERROR", "message": "eval is dangerous"},
            }
        ]
    }

    def _make_side_effect():
        return [
            _git_diff_result(stdout="changed.py\n"),
            _semgrep_result(semgrep_payload, returncode=1),
        ]

    with patch("subprocess.run") as mock_run, patch(
        "coordinator_core.ops.run_semgrep_scan.shutil.which", return_value="/usr/bin/semgrep"
    ):
        mock_run.side_effect = _make_side_effect()
        first = _run_semgrep_scan({"diff_base": "main"}, repo_root=tmp_path)

    with patch("subprocess.run") as mock_run, patch(
        "coordinator_core.ops.run_semgrep_scan.shutil.which", return_value="/usr/bin/semgrep"
    ):
        mock_run.side_effect = _make_side_effect()
        second = _run_semgrep_scan({"diff_base": "main"}, repo_root=tmp_path)

    assert first == second

"""
coordinator_core.ops.test_run_pip_audit

Characterization tests for the "ci.run_pip_audit" op
(coordinator_core.ops.run_pip_audit) — the pip-audit external-tool wrapper
replacing the coordinator-claude agents/dep-cve-auditor.md:68 fence.

subprocess.run is monkeypatched throughout: these tests must not require
pip-audit to actually be installed (per the plan's chunk design note).

Coverage:
  (a) registered under exactly "ci.run_pip_audit" on import
  (b) missing/blank lockfile_path raises ValueError naming the param
  (c) nonexistent lockfile_path raises ValueError
  (d) happy path: pip-audit JSON payload flattened into findings,
      vulnerable_count == len(findings), extra_index_detected reflects the
      caller-supplied param (never re-derived from the lockfile)
  (e) clean/no-vulnerabilities payload → empty findings, count 0
  (f) non-JSON stdout (garbled/empty-tool-crash output) raises RuntimeError
  (g) extra_index_url, when supplied, is forwarded as --extra-index-url in
      the invoked argv; when absent, no such flag appears
  (h) invocation is a direct list-argv subprocess of sys.executable — never
      a shell string (shell=False by construction; no bash/sh anywhere)
  (i) idempotency (AC7): double invocation with identical inputs and an
      identical mocked subprocess result returns an identical response

Spec backlink: pln-coordinator-ops-buildout-from--903224
§ Wave 2 (run cluster)
"""

from __future__ import annotations

import json
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.run_pip_audit  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.run_pip_audit import _run_pip_audit

_OP_NAME = "ci.run_pip_audit"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.run_pip_audit @register_op did not fire"
)


class _FakeCompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


_SAMPLE_PAYLOAD = {
    "dependencies": [
        {
            "name": "requests",
            "version": "2.25.0",
            "vulns": [
                {
                    "id": "PYSEC-2021-1",
                    "fix_versions": ["2.25.1"],
                    "description": "example vuln",
                }
            ],
        },
        {
            "name": "click",
            "version": "8.0.0",
            "vulns": [],
        },
    ]
}

_CLEAN_PAYLOAD = {"dependencies": [{"name": "click", "version": "8.0.0", "vulns": []}]}


def _make_lockfile(tmp_path):
    lockfile = tmp_path / "requirements.lock"
    lockfile.write_text("requests==2.25.0\nclick==8.0.0\n", encoding="utf-8")
    return lockfile


def test_op_registered():
    assert _OP_NAME in _REGISTRY


def test_missing_lockfile_path_raises_value_error():
    with pytest.raises(ValueError, match="lockfile_path"):
        _run_pip_audit({})


def test_blank_lockfile_path_raises_value_error():
    with pytest.raises(ValueError, match="lockfile_path"):
        _run_pip_audit({"lockfile_path": "   "})


def test_nonexistent_lockfile_path_raises_value_error(tmp_path):
    missing = tmp_path / "does-not-exist.lock"
    with pytest.raises(ValueError, match="does not exist"):
        _run_pip_audit({"lockfile_path": str(missing)})


def test_happy_path_flattens_findings(tmp_path, monkeypatch):
    lockfile = _make_lockfile(tmp_path)
    captured_cmd = {}

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeCompletedProcess(stdout=json.dumps(_SAMPLE_PAYLOAD), returncode=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _run_pip_audit({"lockfile_path": str(lockfile)})

    assert result["findings"] == [
        {
            "package": "requests",
            "version": "2.25.0",
            "id": "PYSEC-2021-1",
            "fix_versions": ["2.25.1"],
            "description": "example vuln",
        }
    ]
    assert result["vulnerable_count"] == 1
    assert result["extra_index_detected"] is False
    assert "-m" in captured_cmd["cmd"] and "pip_audit" in captured_cmd["cmd"]


def test_clean_payload_returns_empty_findings(tmp_path, monkeypatch):
    lockfile = _make_lockfile(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: _FakeCompletedProcess(
            stdout=json.dumps(_CLEAN_PAYLOAD), returncode=0
        ),
    )

    result = _run_pip_audit({"lockfile_path": str(lockfile)})

    assert result == {
        "findings": [],
        "vulnerable_count": 0,
        "extra_index_detected": False,
    }


def test_non_json_stdout_raises_runtime_error(tmp_path, monkeypatch):
    lockfile = _make_lockfile(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: _FakeCompletedProcess(
            stdout="not json at all", stderr="boom", returncode=2
        ),
    )

    with pytest.raises(RuntimeError, match="non-JSON"):
        _run_pip_audit({"lockfile_path": str(lockfile)})


def test_empty_stdout_with_nonzero_exit_raises_runtime_error(tmp_path, monkeypatch):
    """Review: code-reviewer (F1, P1) — empty stdout + non-zero exit (tool
    absent/crashed, e.g. "No module named pip_audit") previously fell
    through to the falsy-stdout branch and silently substituted a
    clean-scan payload, indistinguishable from a genuine zero-vuln result.
    This must raise, not return a false-clean result."""
    lockfile = _make_lockfile(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: _FakeCompletedProcess(
            stdout="", stderr="No module named pip_audit", returncode=1
        ),
    )

    with pytest.raises(RuntimeError, match="invocation failure"):
        _run_pip_audit({"lockfile_path": str(lockfile)})


def test_empty_stdout_with_zero_exit_is_treated_as_clean(tmp_path, monkeypatch):
    """Empty stdout paired with a zero exit code is a legitimate clean scan,
    not an invocation failure — the F1 fix only guards the non-zero-exit
    shape."""
    lockfile = _make_lockfile(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: _FakeCompletedProcess(stdout="", stderr="", returncode=0),
    )

    result = _run_pip_audit({"lockfile_path": str(lockfile)})

    assert result == {"findings": [], "vulnerable_count": 0, "extra_index_detected": False}


def test_extra_index_url_forwarded_when_present(tmp_path, monkeypatch):
    lockfile = _make_lockfile(tmp_path)
    captured_cmd = {}

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeCompletedProcess(stdout=json.dumps(_CLEAN_PAYLOAD), returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _run_pip_audit(
        {
            "lockfile_path": str(lockfile),
            "extra_index_url": "https://download.pytorch.org/whl/cu121",
        }
    )

    assert result["extra_index_detected"] is True
    cmd = captured_cmd["cmd"]
    assert "--extra-index-url" in cmd
    idx = cmd.index("--extra-index-url")
    assert cmd[idx + 1] == "https://download.pytorch.org/whl/cu121"


def test_extra_index_url_absent_omits_flag(tmp_path, monkeypatch):
    lockfile = _make_lockfile(tmp_path)
    captured_cmd = {}

    def fake_run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return _FakeCompletedProcess(stdout=json.dumps(_CLEAN_PAYLOAD), returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    _run_pip_audit({"lockfile_path": str(lockfile)})

    assert "--extra-index-url" not in captured_cmd["cmd"]


def test_invocation_is_list_argv_of_sys_executable_no_shell(tmp_path, monkeypatch):
    lockfile = _make_lockfile(tmp_path)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess(stdout=json.dumps(_CLEAN_PAYLOAD), returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    _run_pip_audit({"lockfile_path": str(lockfile)})

    assert isinstance(captured["cmd"], list)
    assert "shell" not in captured["kwargs"] or captured["kwargs"]["shell"] is False
    assert not any(arg in ("bash", "sh") for arg in captured["cmd"])


def test_subprocess_run_carries_timeout(tmp_path, monkeypatch):
    """Review: code-reviewer (F2, P1) — pip-audit is a live network call
    (advisory endpoint); an unresponsive network must not wedge the op
    forever. Assert a timeout= is passed on every invocation."""
    lockfile = _make_lockfile(tmp_path)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess(stdout=json.dumps(_CLEAN_PAYLOAD), returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    _run_pip_audit({"lockfile_path": str(lockfile)})

    assert "timeout" in captured["kwargs"]
    assert captured["kwargs"]["timeout"] and captured["kwargs"]["timeout"] > 0


def test_subprocess_timeout_raises_runtime_error(tmp_path, monkeypatch):
    lockfile = _make_lockfile(tmp_path)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="timed out"):
        _run_pip_audit({"lockfile_path": str(lockfile)})


def test_double_invocation_identical_result(tmp_path, monkeypatch):
    """AC7 idempotency proof: pip-audit is read-only against the lockfile and
    the advisory database — two calls with identical inputs and an
    identical (mocked) tool result return an identical response."""
    lockfile = _make_lockfile(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: _FakeCompletedProcess(
            stdout=json.dumps(_SAMPLE_PAYLOAD), returncode=1
        ),
    )
    params = {"lockfile_path": str(lockfile)}

    first = _run_pip_audit(params)
    second = _run_pip_audit(params)

    assert first == second

"""test_reap_integrated_review_findings_native — pytest coverage for
reap-integrated-review-findings.py's `_reap_native` DR-215 dispatch path.

Spec backlink: state/review-trail/findings/2026-07-17-codereview-sliceslicebigport-wave-a-v1-batch-coordinator-bin-verify-subagent-sandbox-.md
Finding 4 — the native-dispatch branch (the actual DR-215 payload of this
port item) had zero automated test coverage: `test-reap-integrated-review-findings.sh`
forces `COORDINATOR_FORCE_LEGACY=1` on every assertion (or a bogus
`CLAUDE_KLABAUTER_ROOT` for the seam-absent case), so none of `_reap_native`'s four
outcome branches were ever exercised.

Coverage (stubs `cc_invoke.cc_invoke` directly rather than forcing legacy):
  test_reap_native_success_op_exit_0
  test_reap_native_partial_op_exit_2
  test_reap_native_unrecognized_op_exit
  test_reap_native_transport_runtime_error_squashed_to_exit_0
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# ---------------------------------------------------------------------------
# Module import — filename uses hyphens AND a .sh polyglot-trampoline
# extension, so importlib.util file-path loading is required (mirrors
# test_check_install_divergence.py's pattern for hyphenated bin/ scripts).
# ---------------------------------------------------------------------------
_BIN_DIR = Path(__file__).parent.parent


def _load_reap_module():
    # Explicit SourceFileLoader: spec_from_file_location can't infer a loader
    # for a .sh polyglot-trampoline path, leaving spec.loader None (mirrors
    # test_cross_repo_memo.py's pattern for the extensionless dispatcher).
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(
        "reap_integrated_review_findings", str(_BIN_DIR / "reap-integrated-review-findings.py")
    )
    spec = importlib.util.spec_from_loader("reap_integrated_review_findings", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_mod = _load_reap_module()


@pytest.fixture()
def git_repo(tmp_path, monkeypatch):
    """A hermetic git-init'd repo, chdir'd into — `_reap_native` resolves the
    repo root via `git rev-parse --show-toplevel` against the process cwd."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _stub_cc_invoke(monkeypatch, *, result: Dict[str, Any] = None, raises: Exception = None):
    def _fake(op_name, params, repo_root, _claude_klabauter_root=None):
        assert op_name == "fleet.reap_integrated_findings"
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(_mod.cc_invoke, "cc_invoke", _fake)


def test_reap_native_success_op_exit_0(git_repo, monkeypatch, capsys):
    _stub_cc_invoke(monkeypatch, result={"exit_code": 0, "reaped": ["a.md"], "failed": []})

    rc = _mod._reap_native(dry_run=False, commit_prefix="")

    assert rc == 0
    out = capsys.readouterr()
    assert "fleet.reap_integrated_findings completed" in out.out
    assert "1 sidecar(s) reaped" in out.out


def test_reap_native_partial_op_exit_2(git_repo, monkeypatch, capsys):
    _stub_cc_invoke(
        monkeypatch,
        result={"exit_code": 2, "reaped": ["a.md"], "failed": ["b.md"]},
    )

    rc = _mod._reap_native(dry_run=False, commit_prefix="")

    assert rc == 0
    out = capsys.readouterr()
    assert "WARN" in out.err
    assert "partial (exit_code=2" in out.err
    assert "reaped=1" in out.err
    assert "failed=1" in out.err


def test_reap_native_unrecognized_op_exit(git_repo, monkeypatch, capsys):
    _stub_cc_invoke(monkeypatch, result={"exit_code": 9, "reaped": [], "failed": []})

    rc = _mod._reap_native(dry_run=False, commit_prefix="")

    assert rc == 0
    out = capsys.readouterr()
    assert "exit_code=9" in out.out


def test_reap_native_transport_runtime_error_squashed_to_exit_0(git_repo, monkeypatch, capsys):
    _stub_cc_invoke(monkeypatch, raises=RuntimeError("transport down"))

    rc = _mod._reap_native(dry_run=False, commit_prefix="")

    assert rc == 0
    out = capsys.readouterr()
    assert "WARN" in out.err
    assert "transport error" in out.err
    assert "skipping (non-blocking)" in out.err


# ---------------------------------------------------------------------------
# F8 — --summary: raw JSON dump suppressed, a capped sample printed instead;
# summary_limit is forwarded to the op as a param.
# ---------------------------------------------------------------------------


def test_reap_native_summary_passes_limit_param_and_suppresses_raw_json(git_repo, monkeypatch, capsys):
    seen_params = {}

    def _fake(op_name, params, repo_root, _claude_klabauter_root=None):
        seen_params.update(params)
        return {
            "exit_code": 0,
            "dry_run": True,
            "candidates": [{"id": "a.md", "note": "n"}, {"id": "b.md", "note": "n"}],
            "candidates_total": 5,
            "reaped": [],
            "skipped": [],
            "failed": [],
        }

    monkeypatch.setattr(_mod.cc_invoke, "cc_invoke", _fake)

    rc = _mod._reap_native(
        dry_run=True, commit_prefix="", summary=True, summary_limit=2
    )

    assert rc == 0
    assert seen_params.get("summary_limit") == 2
    out = capsys.readouterr()
    assert '"exit_code"' not in out.out, "raw JSON blob must be suppressed under --summary"
    assert "[summary] candidate: a.md" in out.out
    assert "[summary] candidate: b.md" in out.out
    assert "3 more candidate(s)" in out.out
    assert "5 integrated review-findings sidecar(s) would be reaped" in out.out


def test_reap_native_no_summary_omits_limit_param(git_repo, monkeypatch, capsys):
    seen_params = {}

    def _fake(op_name, params, repo_root, _claude_klabauter_root=None):
        seen_params.update(params)
        return {"exit_code": 0, "dry_run": True, "candidates": [], "reaped": [], "skipped": [], "failed": []}

    monkeypatch.setattr(_mod.cc_invoke, "cc_invoke", _fake)

    rc = _mod._reap_native(dry_run=True, commit_prefix="")

    assert rc == 0
    assert "summary_limit" not in seen_params, "summary_limit must be omitted when --summary is not set"

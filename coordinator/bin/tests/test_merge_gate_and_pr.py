"""test_merge_gate_and_pr — pytest tests for merge-gate-and-pr.py.

Spec backlink: docs/plans/2026-07-21-doe-skill-bash-to-claude-klabauter-python-port.md
  (M3 chunk MTM-2). Source: example-doctrine-repo
  coordinator/skills/merging-to-main/SKILL.md §§ Step 1.5, Step 1.65, Step 4.

Coverage (C10, docs/plans/2026-08-05-coverage-gate-planning-artifact-class.md
— VERDICT=UNCOVERED no longer exists; below-threshold is VERDICT=WARN, which
never halts):
  coverage-gate:
    - VERDICT=COVERED passes through the underlying exit code (0).
    - VERDICT=WARN passes through the underlying exit code (0) — never
      halts — and prints the coordinator:review-code remediation offer.
    - VERDICT=WARN + --override still exits 0 and notes the flag is a no-op
      (nothing left to override).
    - VERDICT=WARN + COORDINATOR_OVERRIDE_COVERAGE_GATE=1 still exits 0 and
      notes the same no-op.
    - INDETERMINATE (exit 2, no VERDICT= in stdout) propagates the exit code.
  pr-body:
    - composes ship verdict + release notes + commit log, omits demo path
      section when absent.
    - includes demo path section when present.
  active-branch-guard:
    - --force always exits 0 without calling gh.
    - commit younger than 5 minutes halts (exit 1).
    - commit older than 5 minutes passes (exit 0).
    - gh failure (non-zero / empty output) halts (exit 1).
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "merge_gate_and_pr",
        _BIN_DIR / "merge-gate-and-pr.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# coverage-gate
# ---------------------------------------------------------------------------

def _run_gate(monkeypatch, returncode, stdout, stderr, argv):
    monkeypatch.setattr(
        _mod,
        "_run_review_coverage_gate",
        lambda range_arg: (returncode, stdout, stderr),
    )
    return _mod.main(["coverage-gate", *argv])


def test_coverage_gate_covered_passes(monkeypatch, capsys):
    rc = _run_gate(
        monkeypatch, 0,
        "range=abc..HEAD chain_commits=3 covered=3 uncovered=0 VERDICT=COVERED\n",
        "",
        [],
    )
    assert rc == 0


def test_coverage_gate_warn_does_not_halt(monkeypatch, capsys, clean_override_env):
    """C10: below-threshold is VERDICT=WARN, exit_code 0 from the underlying
    gate — this subcommand must pass that through unchanged, never resolving
    to a halt (pre-C10 this same fixture, spelled VERDICT=UNCOVERED, halted
    with exit 1 — see git history)."""
    rc = _run_gate(
        monkeypatch, 0,
        "range=abc..HEAD chain_commits=3 covered=1 uncovered=2 "
        "coverage_ratio=0.33 VERDICT=WARN\n",
        "uncovered: deadbeef some commit\n",
        [],
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARN: review-coverage gate below threshold" in captured.err
    assert "Remediation offer: dispatch coordinator:review-code" in captured.err


def test_coverage_gate_warn_flag_override_is_noop(monkeypatch, capsys, clean_override_env):
    """--override is accepted for backward compatibility but does nothing —
    WARN already exits 0; there is nothing left to bypass (C10 anti-scope:
    no widened/new override surface)."""
    rc = _run_gate(
        monkeypatch, 0,
        "range=abc..HEAD chain_commits=3 covered=1 uncovered=2 "
        "coverage_ratio=0.33 VERDICT=WARN\n",
        "uncovered: deadbeef some commit\n",
        ["--override"],
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "has no effect — the coverage gate no longer halts" in captured.err


def test_coverage_gate_warn_env_override_is_noop(monkeypatch, capsys, clean_override_env):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_COVERAGE_GATE", "1")
    rc = _run_gate(
        monkeypatch, 0,
        "range=abc..HEAD chain_commits=3 covered=1 uncovered=2 "
        "coverage_ratio=0.33 VERDICT=WARN\n",
        "uncovered: deadbeef some commit\n",
        [],
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "has no effect — the coverage gate no longer halts" in captured.err


def test_coverage_gate_indeterminate_propagates(monkeypatch, capsys):
    rc = _run_gate(
        monkeypatch, 2,
        "",
        "note: could not resolve merge-base\n",
        [],
    )
    assert rc == 2


# ---------------------------------------------------------------------------
# pr-body
# ---------------------------------------------------------------------------

def test_pr_body_without_demo_path(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_commit_log", lambda commit_range: "abc123 first commit")
    rc = _mod.main([
        "pr-body",
        "--ship-verdict", "**Ship verdict:** ship — all green",
        "--release-notes", "## v1.2.3 — 2026-07-23\n### Fixed\n- thing",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Ship verdict" in out
    assert "### Fixed" in out
    assert "Demo Path" not in out
    assert "<summary>Commit log</summary>" in out
    assert "abc123 first commit" in out


def test_pr_body_with_demo_path(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_commit_log", lambda commit_range: "abc123 first commit")
    rc = _mod.main([
        "pr-body",
        "--ship-verdict", "**Ship verdict:** ship",
        "--release-notes", "## v1.0.0",
        "--demo-path", "### Demo Path\n**Setup:** none",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "### Demo Path" in out


# ---------------------------------------------------------------------------
# active-branch-guard
# ---------------------------------------------------------------------------

def test_active_branch_guard_force_skips_gh(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("gh should not be called when --force is set")

    monkeypatch.setattr(_mod, "_gh_pr_view_json", _fail)
    rc = _mod.main(["active-branch-guard", "--pr", "123", "--force"])
    assert rc == 0


def test_active_branch_guard_recent_commit_halts(monkeypatch, capsys):
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    recent_iso = (now - datetime.timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _fake_gh(pr, jq_field):
        if jq_field == "commits[-1].committedDate":
            return 0, recent_iso
        return 0, "work/machine/2026-07-23"

    monkeypatch.setattr(_mod, "_gh_pr_view_json", _fake_gh)
    rc = _mod.main(["active-branch-guard", "--pr", "123"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "younger than 5 minutes" in err
    assert "work/machine/2026-07-23" in err


def test_active_branch_guard_settled_commit_passes(monkeypatch):
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    old_iso = (now - datetime.timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _fake_gh(pr, jq_field):
        if jq_field == "commits[-1].committedDate":
            return 0, old_iso
        return 0, "work/machine/2026-07-23"

    monkeypatch.setattr(_mod, "_gh_pr_view_json", _fake_gh)
    rc = _mod.main(["active-branch-guard", "--pr", "123"])
    assert rc == 0


def test_active_branch_guard_gh_failure_halts(monkeypatch, capsys):
    def _fake_gh(pr, jq_field):
        return 1, ""

    monkeypatch.setattr(_mod, "_gh_pr_view_json", _fake_gh)
    rc = _mod.main(["active-branch-guard", "--pr", "123"])
    assert rc == 1
    assert "could not read commit timestamps" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.fixture
def clean_override_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_OVERRIDE_COVERAGE_GATE", raising=False)

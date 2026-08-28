"""test_merge_gate_and_pr — pytest tests for merge-gate-and-pr.py.

Spec backlink: docs/plans/2026-07-21-doe-skill-bash-to-claude-klabauter-python-port.md [DEAD-CITATION: plan file never committed to this repo]
  (M3 chunk MTM-2). Source: DoE-claude
  coordinator/skills/merging-to-main/SKILL.md §§ Step 1.5, Step 1.65, Step 4.

K-001 (state/kill-ledger.md): the `coverage-gate` subcommand this file used
to test was removed — its sole job was relaying `review-coverage-gate.py`'s
VERDICT line, and the verdict computation it wrapped is dead (WARN on 40 of
40 closes, zero effect). See merge-gate-and-pr.py's own docstring.

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
# coverage-gate
# ---------------------------------------------------------------------------
#
# The relay this subcommand restores per docs/plans/2026-08-27-the-merge-gate-
# is-pointed-back-at-the-coverage-engine.md § C1: it calls
# gate.validate_invocable's "review" dimension (gate_dimension_review.py) and
# refuses on FAIL. `_run_gate_validate_invocable` is the isolation seam so
# these tests never touch the real engine, git, or the review-trail corpus.


def test_coverage_gate_no_changed_files_passes(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_changed_files", lambda commit_range: [])

    def _fail(*args, **kwargs):
        raise AssertionError("gate.validate_invocable should not be called for an empty diff")

    monkeypatch.setattr(_mod, "_run_gate_validate_invocable", _fail)
    rc = _mod.main(["coverage-gate"])
    assert rc == 0
    assert "nothing to check" in capsys.readouterr().out


def test_coverage_gate_covered_passes(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_changed_files", lambda commit_range: ["a.py"])
    monkeypatch.setattr(
        _mod,
        "_run_gate_validate_invocable",
        lambda changed_files, diff_base, repo_root: {
            "dimensions": [
                {
                    "dimension": "review",
                    "verdict": "PASS",
                    "detail": "covered: all 1 commit(s) touching changed_files are review-trail stamped",
                }
            ]
        },
    )
    rc = _mod.main(["coverage-gate"])
    assert rc == 0
    assert "covered" in capsys.readouterr().out


def test_coverage_gate_uncovered_refuses(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_changed_files", lambda commit_range: ["a.py"])
    monkeypatch.setattr(
        _mod,
        "_run_gate_validate_invocable",
        lambda changed_files, diff_base, repo_root: {
            "dimensions": [
                {
                    "dimension": "review",
                    "verdict": "FAIL",
                    "detail": "uncovered: 1/2 commit(s) touching changed_files have no review-trail stamp (e.g. deadbeef1234)",
                }
            ]
        },
    )
    rc = _mod.main(["coverage-gate"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "deadbeef1234" in err
    # C4 flipped this note when the ruleset went up and flipped it BACK when the
    # ruleset was withdrawn the same day. The note tracks what is actually
    # enforced on the remote, so it is only true while a ruleset exists -- do
    # not re-word it to the server-side claim without checking
    # `GET /repos/dbc-example-operator/claude-klabauter/rulesets` is non-empty first.
    assert "not enforced at the git-push layer" in err


def test_coverage_gate_dimension_unavailable_passes(monkeypatch, capsys):
    monkeypatch.setattr(_mod, "_changed_files", lambda commit_range: ["a.py"])
    monkeypatch.setattr(
        _mod,
        "_run_gate_validate_invocable",
        lambda changed_files, diff_base, repo_root: {
            "dimensions": [
                {
                    "dimension": "review",
                    "verdict": "UNAVAILABLE",
                    "detail": "review-trail corpus unavailable: boom",
                }
            ]
        },
    )
    rc = _mod.main(["coverage-gate"])
    assert rc == 0
    assert "unavailable" in capsys.readouterr().out

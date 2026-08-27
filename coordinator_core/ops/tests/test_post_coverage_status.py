"""
coordinator_core.ops.tests.test_post_coverage_status

Tests for `post_coverage_status.compute_status()` -- C1's WHITELIST mapping
from `gate.validate_invocable`'s "review" dimension verdict onto a GitHub
commit status `state` (docs/plans/2026-08-27-the-merge-gate-gets-a-remote-
authority-layer.md § C1, dispatched as C3 here).

Covers, per the C3 dispatch brief:
  - verdict ERROR, UNAVAILABLE, and SKIPPED each post `state=failure` naming
    which state fired -- the whitelist (only `PASS` -> success), not an
    exit-code-shaped blacklist.
  - a missing `review` key does NOT post `state=success`.
  - `_changed_files_or_git_failure` on a git failure (nonzero returncode)
    does NOT post `state=success` and is distinguished from a genuine empty
    range (no changed files).
  - a FAIL verdict posts `state=failure`.

Does NOT re-test the coverage engine itself (`gate.validate_invocable` /
`_run_gate_validate_invocable`) -- that has its own suite
(`coordinator/bin/tests/test_merge_gate_and_pr.py` and
`coordinator_core/ops/tests/test_review_coverage_core.py`). Here,
`_load_merge_gate_module()` is monkeypatched to a stub carrying only
`_run_gate_validate_invocable`, so these tests exercise `compute_status`'s
OWN mapping logic in isolation.

Spec backlink: docs/plans/2026-08-27-the-merge-gate-gets-a-remote-authority-
layer.md § C1/C3.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops import post_coverage_status as pcs_mod

pytestmark = [pytest.mark.cadence]


class _StubMergeGateModule:
    """Stand-in for the real `merge-gate-and-pr.py` module, carrying only
    the one function `compute_status` consumes -- `_run_gate_validate_invocable`.
    Avoids re-executing (or re-testing) the real coverage engine."""

    def __init__(self, result: dict):
        self._result = result

    def _run_gate_validate_invocable(self, changed_files, commit_range, root):
        return self._result


def _dimensions_result(review_dict):
    if review_dict is None:
        return {"dimensions": []}
    return {"dimensions": [{"dimension": "review", **review_dict}]}


def _patch_gate(monkeypatch, review_dict):
    stub = _StubMergeGateModule(_dimensions_result(review_dict))
    monkeypatch.setattr(pcs_mod, "_load_merge_gate_module", lambda: stub)


def _patch_changed_files(monkeypatch, files, git_failed=False):
    monkeypatch.setattr(
        pcs_mod, "_changed_files_or_git_failure", lambda commit_range: (files, git_failed)
    )


# ---------------------------------------------------------------------------
# Whitelist: only verdict == "PASS" reaches state=success.
# ---------------------------------------------------------------------------


def test_verdict_pass_posts_success(monkeypatch):
    _patch_changed_files(monkeypatch, ["a.py"])
    _patch_gate(monkeypatch, {"verdict": "PASS", "detail": "covered"})

    state, description = pcs_mod.compute_status("main..HEAD")

    assert state == "success"
    assert "covered" in description


@pytest.mark.parametrize("verdict", ["ERROR", "UNAVAILABLE", "SKIPPED"])
def test_verdict_error_unavailable_skipped_each_post_failure_naming_state(monkeypatch, verdict):
    """The whitelist, not the exit-code blacklist: every non-PASS verdict
    maps to `state=failure`, and the description names WHICH verdict fired
    so an operator reading the GitHub status check does not have to guess."""
    _patch_changed_files(monkeypatch, ["a.py"])
    _patch_gate(monkeypatch, {"verdict": verdict, "detail": "x"})

    state, description = pcs_mod.compute_status("main..HEAD")

    assert state == "failure"
    assert verdict in description


def test_verdict_fail_posts_failure(monkeypatch):
    _patch_changed_files(monkeypatch, ["a.py"])
    _patch_gate(monkeypatch, {"verdict": "FAIL", "detail": "uncovered lines"})

    state, description = pcs_mod.compute_status("main..HEAD")

    assert state == "failure"
    assert "FAIL" in description


def test_missing_review_key_does_not_post_success(monkeypatch):
    """`review` absent from `dimensions` entirely (not merely a falsy
    verdict) must still map to `state=failure` -- a missing dimension is
    not evidence of coverage."""
    _patch_changed_files(monkeypatch, ["a.py"])
    _patch_gate(monkeypatch, None)

    state, description = pcs_mod.compute_status("main..HEAD")

    assert state == "failure"
    assert "review" in description


# ---------------------------------------------------------------------------
# `_changed_files_or_git_failure`: a git failure must never read as an
# empty (and therefore benign-but-indeterminate) changed-files list.
# ---------------------------------------------------------------------------


def test_changed_files_git_failure_does_not_post_success(monkeypatch):
    """A nonzero `git diff` returncode must short-circuit to `state=failure`
    BEFORE the gate is ever consulted -- distinguished from a genuine empty
    range, which is also `failure` but for a different, distinguishable
    reason (see the next test)."""
    _patch_changed_files(monkeypatch, [], git_failed=True)
    gate_calls = []
    stub = _StubMergeGateModule(_dimensions_result({"verdict": "PASS"}))
    stub._run_gate_validate_invocable = lambda *a, **kw: gate_calls.append(1)
    monkeypatch.setattr(pcs_mod, "_load_merge_gate_module", lambda: stub)

    state, description = pcs_mod.compute_status("main..HEAD")

    assert state == "failure"
    assert "git diff failed" in description
    assert gate_calls == [], "a git failure must short-circuit before consulting the gate's verdict engine"


def test_changed_files_genuine_empty_range_is_distinguished_from_git_failure(monkeypatch):
    """An empty-but-successful `git diff` (genuinely no changed files in
    range) is a DIFFERENT reason string from a git failure -- both map to
    `state=failure`, but they must not be conflated: this poster's
    whitelist requires an earned PASS, not merely the absence of a git
    error."""
    _patch_changed_files(monkeypatch, [], git_failed=False)
    gate_calls = []
    stub = _StubMergeGateModule(_dimensions_result({"verdict": "PASS"}))
    stub._run_gate_validate_invocable = lambda *a, **kw: gate_calls.append(1)
    monkeypatch.setattr(pcs_mod, "_load_merge_gate_module", lambda: stub)

    state, description = pcs_mod.compute_status("main..HEAD")

    assert state == "failure"
    assert "no changed files" in description
    assert "git diff failed" not in description
    assert gate_calls == [], "an empty range must also short-circuit before consulting the gate's verdict engine"

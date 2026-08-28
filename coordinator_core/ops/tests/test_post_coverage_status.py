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

Also covers `resolve_token()`'s full ladder, which shipped with C1 untested
-- the gap that let a fail-closed-on-a-present-credential defect through 15
green tests. In particular: a `hosts.yml` in SECURE-STORAGE shape (host
entry present, no `oauth_token` key) must fall THROUGH leg 3 rather than
resolve to None, because that is the shape every keyring-backed `gh auth
login` writes.

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


# ---------------------------------------------------------------------------
# Token ladder: env -> hosts.yml -> Windows Credential Manager, fail closed.
# ---------------------------------------------------------------------------


def _isolate_ladder(monkeypatch, *, hosts_entry=None, cred_token=None):
    """Cut `resolve_token`'s three lower legs off the live machine.

    Every token test runs against injected state -- never this box's real
    environment, real `hosts.yml`, or real credential store.
    """
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(pcs_mod, "_gh_github_host_entry", lambda: hosts_entry)
    monkeypatch.setattr(
        pcs_mod, "_token_from_windows_credential_manager", lambda: cred_token
    )


@pytest.mark.parametrize("env_name", ["GITHUB_TOKEN", "GH_TOKEN"])
def test_env_token_wins_over_every_lower_leg(monkeypatch, env_name):
    _isolate_ladder(
        monkeypatch, hosts_entry={"oauth_token": "from-hosts"}, cred_token="from-cred"
    )
    monkeypatch.setenv(env_name, "from-env")

    assert pcs_mod.resolve_token() == "from-env"


def test_github_token_outranks_gh_token(monkeypatch):
    _isolate_ladder(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "primary")
    monkeypatch.setenv("GH_TOKEN", "secondary")

    assert pcs_mod.resolve_token() == "primary"


def test_hosts_file_oauth_token_resolves_without_reaching_the_credential_store(
    monkeypatch,
):
    calls = []
    _isolate_ladder(monkeypatch, hosts_entry={"oauth_token": "from-hosts"})
    monkeypatch.setattr(
        pcs_mod,
        "_token_from_windows_credential_manager",
        lambda: calls.append(1) or "from-cred",
    )

    assert pcs_mod.resolve_token() == "from-hosts"
    assert calls == [], "leg 4 must not be consulted once leg 3 has produced a token"


def test_secure_storage_hosts_shape_falls_through_to_the_credential_store(monkeypatch):
    """THE REGRESSION THIS SUITE EXISTS FOR.

    A keyring-backed `gh auth login` writes a `github.com` entry carrying
    `users:`/`user:` and NO `oauth_token`. Leg 3 must report "no token here",
    not "no token anywhere" -- the file parses fine, so a `return None` at
    that point is a fail-closed on a credential that IS present, and once
    the ruleset is live that refuses every push to `main`.
    """
    _isolate_ladder(
        monkeypatch,
        hosts_entry={"users": {"dbc-example-operator": None}, "user": "dbc-example-operator"},
        cred_token="from-cred",
    )

    assert pcs_mod._token_from_gh_hosts_file() is None
    assert pcs_mod.resolve_token() == "from-cred"


def test_no_token_anywhere_resolves_none(monkeypatch):
    _isolate_ladder(monkeypatch)

    assert pcs_mod.resolve_token() is None


def test_no_token_posts_nothing_rather_than_assuming_covered(monkeypatch):
    _isolate_ladder(monkeypatch)
    computed = []
    monkeypatch.setattr(
        pcs_mod, "compute_status", lambda *a, **kw: computed.append(1) or ("success", "")
    )

    result = pcs_mod.post_coverage_status("o", "r", "sha", "main..HEAD")

    assert result.posted is False
    assert result.state is None
    assert "no GitHub token" in result.reason
    assert computed == [], "an unpostable run must not spend the verdict computation"


def test_credential_manager_leg_is_inert_off_windows(monkeypatch):
    monkeypatch.setattr(pcs_mod.sys, "platform", "linux")

    assert pcs_mod._token_from_windows_credential_manager() is None


@pytest.mark.parametrize(
    "blob,expected",
    [
        (b"gho_abc123", "gho_abc123"),
        ("gho_abc123".encode("utf-16-le"), "gho_abc123"),
        (b"  gho_abc123  ", "gho_abc123"),
        (b"", None),
        (b"\x00\x01\x02", None),
    ],
)
def test_credential_blob_decodes_both_encodings_and_fails_closed_on_junk(blob, expected):
    assert pcs_mod._decode_credential_blob(blob) == expected


def test_active_account_prefers_the_user_key_then_falls_back_to_users(monkeypatch):
    monkeypatch.setattr(
        pcs_mod,
        "_gh_github_host_entry",
        lambda: {"user": "chosen", "users": {"other": None}},
    )
    assert pcs_mod._gh_active_account() == "chosen"

    monkeypatch.setattr(
        pcs_mod, "_gh_github_host_entry", lambda: {"users": {"only": None}}
    )
    assert pcs_mod._gh_active_account() == "only"

    monkeypatch.setattr(pcs_mod, "_gh_github_host_entry", lambda: None)
    assert pcs_mod._gh_active_account() is None

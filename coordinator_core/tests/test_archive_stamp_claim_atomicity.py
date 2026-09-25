"""
coordinator_core.tests.test_archive_stamp_claim_atomicity — coverage for
Track D of P026 (`docs/plans/2026-09-07-a-claim-is-written-twice-and-nothing-
compares-them.md`, C9): AC13's per-write landed/failed map on
`cs_claim_handoff`'s `return_result=True` dict, and AC14's lock-collapse
decision (stay at two acquisitions, per the C8 spike's measured 0.0688 ms
marginal cost — `docs/research/2026-09-claim-write-atomicity-spike.md`).

This module pins:

  1. On a clean claim, `result["writes"]` carries all four keys
     (`handoff_transition`, `pickup`, `session_goal`, `claimant_identity`),
     each `True`.
  2. A forced failure of EACH of the three best-effort writes individually
     is reflected as `False` in its own key — and only its own key; the
     other three still land `True`. No write fails silently (AC13).
  3. The claim transition itself still lands (rc=0) even when every
     best-effort write fails — best-effort stays non-fatal.
  4. `_record_pickup_best_effort`, `_record_session_goal_best_effort`, and
     `_record_claimant_identity_best_effort` each return `bool`, not `None`
     — the per-function contract AC13's map is built from.
  5. AC14: exactly two `locked_rmw` acquisitions occur over the handoff
     frontmatter file during one `cs_claim_handoff` call (write 1 inside
     `handoff_transition._claim`, write 4 inside
     `_record_claimant_identity_best_effort`) — they do not collapse to
     one, matching the spike's recorded decision.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

import coordinator_core.ops.handoff_transition  # noqa: F401 — @register_op side effect
import coordinator_core.ops.session.record_pickup  # noqa: F401 — @register_op side effect

import coordinator_core.archive_stamp as arstamp
from coordinator_core.session import harness_registry
from coordinator_core.tests._fixtures import init_repo as _init_repo
from coordinator_core.tests._fixtures import run_git as _git

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_DEFAULT_TEST_SESSION_ID = "33333333-3333-3333-3333-333333333333"


def _seed_handoff(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: ready_to_fire\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {})
    # Registry read is best-effort and irrelevant to this module's concerns —
    # omit the name field cleanly rather than exercising the registry.
    monkeypatch.setattr(harness_registry, "self_record", lambda: None)
    monkeypatch.setattr(harness_registry, "lookup", lambda sid: None)


class TestAC13WritesMap:
    def test_clean_claim_reports_all_four_writes_landed(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h1.md")

        result = arstamp.cs_claim_handoff(str(hp), return_result=True)

        assert result["exit_code"] == 0
        writes = result["writes"]
        assert writes == {
            "handoff_transition": True,
            "pickup": True,
            "session_goal": True,
            "claimant_identity": True,
        }

    def test_pickup_failure_is_named_and_only_pickup(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h2.md")
        monkeypatch.setattr(arstamp, "_record_pickup_best_effort", lambda *a, **k: False)

        result = arstamp.cs_claim_handoff(str(hp), return_result=True)

        assert result["exit_code"] == 0
        assert result["writes"]["pickup"] is False
        assert result["writes"]["session_goal"] is True
        assert result["writes"]["claimant_identity"] is True
        assert result["writes"]["handoff_transition"] is True

    def test_session_goal_failure_is_named_and_only_session_goal(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h3.md")
        monkeypatch.setattr(arstamp, "_record_session_goal_best_effort", lambda *a, **k: False)

        result = arstamp.cs_claim_handoff(str(hp), return_result=True)

        assert result["exit_code"] == 0
        assert result["writes"]["session_goal"] is False
        assert result["writes"]["pickup"] is True
        assert result["writes"]["claimant_identity"] is True

    def test_claimant_identity_failure_is_named_and_only_claimant_identity(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h4.md")
        monkeypatch.setattr(arstamp, "_record_claimant_identity_best_effort", lambda *a, **k: False)

        result = arstamp.cs_claim_handoff(str(hp), return_result=True)

        assert result["exit_code"] == 0
        assert result["writes"]["claimant_identity"] is False
        assert result["writes"]["pickup"] is True
        assert result["writes"]["session_goal"] is True

    def test_all_three_best_effort_writes_failing_still_lands_the_claim(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h5.md")
        monkeypatch.setattr(arstamp, "_record_pickup_best_effort", lambda *a, **k: False)
        monkeypatch.setattr(arstamp, "_record_session_goal_best_effort", lambda *a, **k: False)
        monkeypatch.setattr(arstamp, "_record_claimant_identity_best_effort", lambda *a, **k: False)

        result = arstamp.cs_claim_handoff(str(hp), return_result=True)

        assert result["exit_code"] == 0
        assert result["writes"] == {
            "handoff_transition": True,
            "pickup": False,
            "session_goal": False,
            "claimant_identity": False,
        }
        text = hp.read_text(encoding="utf-8")
        assert "status: claimed" in text


class TestBestEffortFunctionsReturnBool:
    """AC13's per-function contract: each of the three writer functions
    returns bool, not None — this is what the map above is built from."""

    def test_record_pickup_best_effort_returns_true_on_success(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h6.md")

        result = arstamp._record_pickup_best_effort(str(hp), repo, _DEFAULT_TEST_SESSION_ID)

        assert result is True

    def test_record_session_goal_best_effort_returns_false_with_no_title_or_summary(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "h7.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nstatus: open\n---\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add h7")

        result = arstamp._record_session_goal_best_effort(str(path), repo, _DEFAULT_TEST_SESSION_ID)

        assert result is False

    def test_record_claimant_identity_best_effort_returns_true_on_success(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h8.md")

        result = arstamp._record_claimant_identity_best_effort(
            str(hp), repo, "claimed_by", _DEFAULT_TEST_SESSION_ID
        )

        assert result is True

    def test_record_claimant_identity_best_effort_returns_false_on_unresolvable_repo_root(self, tmp_path):
        # No git repo at all — _git_common_dir returns None on this path.
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        hp = not_a_repo / "h9.md"
        hp.write_text("---\nstatus: open\n---\n\nBody.\n", encoding="utf-8")

        result = arstamp._record_claimant_identity_best_effort(
            str(hp), not_a_repo, "claimed_by", _DEFAULT_TEST_SESSION_ID
        )

        assert result is False


class TestAC14LockAcquisitionCount:
    def test_two_locked_rmw_acquisitions_over_the_handoff_file_stay_two(self, tmp_path):
        """The spike (C8) measured collapsing the two acquisitions at
        0.0688 ms marginal cost against real cross-module coupling and
        recorded "stay at two" as the reason. This pins that the claim path
        still performs exactly two `locked_rmw` calls against the handoff
        frontmatter file — one inside `handoff_transition._claim` (write 1),
        one inside `_record_claimant_identity_best_effort` (write 4) — never
        collapsed to one, and never silently grown to three-plus."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h10.md")

        calls = []
        orig_locked_rmw = arstamp.locked_rmw

        def _counting_locked_rmw(path, *args, **kwargs):
            if Path(path) == hp:
                calls.append(path)
            return orig_locked_rmw(path, *args, **kwargs)

        with mock.patch.object(arstamp, "locked_rmw", side_effect=_counting_locked_rmw):
            # handoff_transition._claim uses its own `locked_rmw` import —
            # patch that module's reference too so both writers are counted.
            import coordinator_core.ops.handoff_transition as _ht

            with mock.patch.object(_ht, "locked_rmw", side_effect=_counting_locked_rmw):
                rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        assert len(calls) == 2

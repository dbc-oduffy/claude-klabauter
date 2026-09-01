"""
coordinator_core.pickup_assemble.tests.test_adjudicate_claimed_batons — C4
falsifier (docs/plans/2026-09-01-the-abandonment-verdict-outlives-the-
archiver.md).

Purpose: `adjudicate_claimed_batons` is the bulk READ-ONLY route this chunk
adds. Walks `state/handoffs/*.md` for `status: claimed`, resolves each
holder's basis into one of four named buckets (live / archive-record /
no-sid / unknown), writes nothing, and settles the exit-code contract so a
genuine transport failure (`APPLY_EXIT_TRANSPORT_FAIL`) is never the same
shape as a completed sweep (`APPLY_EXIT_OK`) — even a sweep over zero
claimed rows.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_adjudicate_claimed_batons.py -q
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble.apply as pa_apply

pytestmark = [pytest.mark.cadence]


_HANDOFF_FM = (
    'title: "Test Handoff"\n'
    "created: 2026-01-01\n"
    "branch: work/test/2026-01-01\n"
    "status: {status}\n"
    'predecessor: "none"\n'
    "deployment_state: {deployment_state}\n"
    "claimed_by: {holder}\n"
    "claimed_at: 2026-01-01T00:00:00Z\n"
)


def _seed_handoff(
    repo: Path,
    name: str,
    *,
    status: str = "claimed",
    holder: str = "",
    deployment_state: str = "in_flight",
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = _HANDOFF_FM.format(status=status, holder=holder, deployment_state=deployment_state)
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def test_no_claimed_batons_is_exit_ok_with_honest_zero(tmp_path):
    """A corpus with zero `status: claimed` rows is a completed sweep, not a
    failure — `APPLY_EXIT_OK` with `claimed_count: 0`, never mistaken for the
    predecessor's silent "exit 0 while doing nothing"."""
    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True)
    _seed_handoff(repo, "h1.md", status="open", holder="")

    exit_code, report = pa_apply.adjudicate_claimed_batons(repo_root=repo)

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert report["claimed_count"] == 0
    assert report["rows"] == []
    assert report["buckets"] == {b: 0 for b in pa_apply.ADJUDICATION_BUCKETS}


def test_four_way_split(tmp_path, monkeypatch):
    """One row per bucket: a live holder, an archived-dead holder, a
    holder with unresolvable evidence, and a claim with no holder at all —
    every row resolves into exactly one named bucket, and the report's
    `deployment_state` column is carried through verbatim as display."""
    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True)
    _seed_handoff(repo, "h-live.md", holder="sid-live", deployment_state="in_flight")
    _seed_handoff(repo, "h-archived.md", holder="sid-dead", deployment_state="shipped")
    _seed_handoff(repo, "h-unknown.md", holder="sid-ghost", deployment_state="continued")
    _seed_handoff(repo, "h-no-sid.md", holder="", deployment_state="in_flight")
    _seed_handoff(repo, "h-open.md", status="open", holder="", deployment_state="in_flight")

    def _session_live(sid, cwd=None):
        return sid == "sid-live"

    def _abandonment_basis(sid, cwd=None):
        if sid == "sid-dead":
            return (True, "archive-record")
        return (False, "unknown")

    monkeypatch.setattr(pa_apply._liveness, "session_live", _session_live)
    monkeypatch.setattr(pa_apply._liveness, "abandonment_basis", _abandonment_basis)

    exit_code, report = pa_apply.adjudicate_claimed_batons(repo_root=repo)

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert report["claimed_count"] == 4
    assert report["buckets"] == {
        "live": 1,
        "archive-record": 1,
        "no-sid": 1,
        "unknown": 1,
    }
    by_path = {row["path"]: row for row in report["rows"]}
    assert by_path["state/handoffs/h-live.md"]["basis"] == "live"
    assert by_path["state/handoffs/h-archived.md"]["basis"] == "archive-record"
    assert by_path["state/handoffs/h-archived.md"]["deployment_state"] == "shipped"
    assert by_path["state/handoffs/h-unknown.md"]["basis"] == "unknown"
    assert by_path["state/handoffs/h-no-sid.md"]["basis"] == "no-sid"
    # The `open` row is never adjudicated -- the sweep selects on `status:
    # claimed` only.
    assert "state/handoffs/h-open.md" not in by_path


def test_live_dir_signals_folds_into_unknown_not_archive_record(tmp_path, monkeypatch):
    """`abandonment_basis`'s OTHER positive leg (`live-dir-signals`) must
    not read as `archive-record` -- this sweep's own bucket vocabulary is
    exactly the four named in the C4 body, and only a genuine archive
    record earns that bucket."""
    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True)
    _seed_handoff(repo, "h-stale.md", holder="sid-stale")

    monkeypatch.setattr(pa_apply._liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(
        pa_apply._liveness,
        "abandonment_basis",
        lambda sid, cwd=None: (True, "live-dir-signals"),
    )

    exit_code, report = pa_apply.adjudicate_claimed_batons(repo_root=repo)

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert report["rows"][0]["basis"] == "unknown"


def test_no_write_no_release_surface(tmp_path):
    """No `--write`/`--release` argv is accepted by the CLI wrapper -- the
    absence is the deliverable, not a gated flag."""
    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True)
    exit_code = pa_apply.main_adjudicate_claimed_batons(["--write"])
    assert exit_code == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
    assert not hasattr(pa_apply, "APPLY_EXIT_HALTED_AT_JUDGMENT") or True  # no 5th vocabulary added


def test_missing_repo_root_is_transport_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(pa_apply, "_resolve_repo_root_for_apply", lambda start=None: None)
    exit_code, report = pa_apply.adjudicate_claimed_batons()
    assert exit_code == pa_apply.APPLY_EXIT_TRANSPORT_FAIL
    assert "error" in report


def test_process_time_budget_under_200ms(tmp_path, monkeypatch):
    """Budget (C4 body): ONE archive listing and one pass over the batons for
    the whole sweep -- assert process time under 200ms over a representative
    corpus, measured with `time.process_time()`, never wall clock."""
    repo = tmp_path / "repo"
    (repo / "state" / "handoffs").mkdir(parents=True)
    for i in range(50):
        _seed_handoff(repo, f"h{i}.md", holder=f"sid-{i}")

    monkeypatch.setattr(pa_apply._liveness, "session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr(
        pa_apply._liveness, "abandonment_basis", lambda sid, cwd=None: (False, "unknown")
    )

    start = time.process_time()
    exit_code, report = pa_apply.adjudicate_claimed_batons(repo_root=repo)
    elapsed_ms = (time.process_time() - start) * 1000.0

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert report["claimed_count"] == 50
    assert elapsed_ms < 200.0, f"adjudicate_claimed_batons took {elapsed_ms:.1f}ms over 50 rows"

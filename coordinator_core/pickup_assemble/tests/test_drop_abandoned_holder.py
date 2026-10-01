"""
coordinator_core.pickup_assemble.tests.test_drop_abandoned_holder — C3 falsifier
(docs/plans/2026-09-01-the-abandonment-verdict-outlives-the-archiver.md).

Purpose: `drop`'s holder gate splits its former blunt "held by someone else ->
refuse" case on `liveness.abandonment_basis`. An abandoned holder (`archive-
record`/`live-dir-signals`) becomes releasable; `unknown`/`no-sid` refuse
exactly as before. A release granted on an abandoned basis is re-verified a
SECOND time, immediately before the class-inverse call, so a holder that
resurrects (or a ledger dir that gets swapped) inside the read-then-mutate
window re-refuses rather than proceeding on the stale gate-time basis. The
dead holder's uncommitted residue is reported (advisory, never blocking) via
the shared `claims._warn_dead_holder_residue` seam.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_drop_abandoned_holder.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.pickup_assemble.apply as pa_apply
from coordinator_core.session.liveness import AbandonmentVerdict as _V

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)


_HANDOFF_FM = (
    'title: "Test Handoff"\n'
    "created: 2026-01-01\n"
    "branch: work/test/2026-01-01\n"
    "status: claimed\n"
    'predecessor: "none"\n'
    "deployment_state: in_flight\n"
    "claimed_by: {holder}\n"
    "claimed_at: 2026-01-01T00:00:00Z\n"
)


def _seed_claimed_handoff(repo: Path, name: str, holder: str = "sid-holder") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n{_HANDOFF_FM.format(holder=holder)}---\n\n# Handoff\n\nBody.\n", encoding="utf-8"
    )
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _write_ledger_claim(repo: Path, basename: str, holder_sid: str) -> Path:
    cdir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    (cdir / "claimed_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")
    (cdir / "stage").write_text("apply\n", encoding="utf-8")
    return cdir


def _rev_count(repo: Path) -> str:
    return _git(repo, "rev-list", "--count", "HEAD").stdout.strip()


def test_abandoned_holder_drop_is_releasable(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h5.md", holder="sid-dead-holder")
    _write_ledger_claim(repo, "h5.md", "sid-dead-holder")
    monkeypatch.chdir(repo)

    monkeypatch.setattr(
        pa_apply._liveness,
        "abandonment_basis",
        lambda sid, cwd=None: _V(True, "archive-record", False)
        if sid == "sid-dead-holder"
        else _V(False, "unknown", False),
    )

    exit_code, report = pa_apply.drop(
        "state/handoffs/h5.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert report["released"] is True
    assert report["abandonment_basis"] == "archive-record"
    text = handoff.read_text(encoding="utf-8")
    assert "claimed_by: sid-dead-holder" not in text


def test_unknown_basis_refuses_like_today(tmp_path, monkeypatch):
    """`unknown` — absent evidence — must keep the claim, unchanged from the
    pre-C3 blunt refusal."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h6.md", holder="sid-live-holder")
    _write_ledger_claim(repo, "h6.md", "sid-live-holder")
    monkeypatch.chdir(repo)

    monkeypatch.setattr(
        pa_apply._liveness, "abandonment_basis", lambda sid, cwd=None: _V(False, "unknown", False)
    )

    before_bytes = handoff.read_bytes()
    before_rev = _rev_count(repo)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h6.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
    assert report["reason"] == "drop_not_holder"
    assert report["abandonment_basis"] == "unknown"
    assert handoff.read_bytes() == before_bytes
    assert _rev_count(repo) == before_rev


def test_no_sid_ledger_reports_no_sid_basis_and_refuses(tmp_path, monkeypatch):
    """The holderless arm: a legacy pid-only ledger dir carries no
    `session_id` at all — `_recorded_claim_session_id` reads `""`, and the
    REAL (unmocked) `abandonment_basis("", cwd)` resolves `(False, "no-sid")`
    by construction (`sid` falsy is its first check). Counted separately from
    an `unknown` (evidence-absent) basis, never folded into the abandoned
    bucket — this arm still refuses."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h7.md", holder="sid-legacy")
    cdir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / "h7.md"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "pid").write_text("999999\n", encoding="utf-8")
    (cdir / "stage").write_text("apply\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    before_bytes = handoff.read_bytes()

    exit_code, report = pa_apply.drop(
        "state/handoffs/h7.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
    assert report["abandonment_basis"] == "no-sid"
    assert handoff.read_bytes() == before_bytes


def test_resurrection_between_gate_and_release_refuses(tmp_path, monkeypatch):
    """Staff-eng review finding 2 — the TOCTOU close. The gate grants on an
    `archive-record` basis; the holder's registry record resolves LIVE by
    the time the recheck runs immediately before the class-inverse call.
    Mutates nothing and re-refuses with `APPLY_EXIT_CLAIM_DENIED`, never
    proceeding on the stale gate-time verdict."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h8.md", holder="sid-dead-holder")
    _write_ledger_claim(repo, "h8.md", "sid-dead-holder")
    monkeypatch.chdir(repo)

    # The holder reads dead at the gate and live at the recheck: one verdict
    # read per moment, so the two reads are the two calls.
    _calls = {"n": 0}

    def _dead_then_live(sid, cwd=None):
        _calls["n"] += 1
        if _calls["n"] == 1:
            return _V(True, "archive-record", False)
        return _V(False, "live", True)

    monkeypatch.setattr(pa_apply._liveness, "abandonment_basis", _dead_then_live)

    before_bytes = handoff.read_bytes()
    before_rev = _rev_count(repo)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h8.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
    assert report["reason"] == "drop_holder_resurrected"
    assert report["abandonment_basis"] == "live"
    assert _calls["n"] == 2
    assert handoff.read_bytes() == before_bytes, "a re-refused drop must not touch the stamp"
    assert _rev_count(repo) == before_rev, "a re-refused drop must land no commit"


def test_ledger_dir_identity_change_between_gate_and_release_refuses(tmp_path, monkeypatch):
    """Staff-eng review finding 2's second clause: the ledger dir's own
    identity/mtime is recorded at gate time and the release aborts if it
    changed — a dir removed and recreated (a fresh claim, by a resurrected
    or a different holder) fails this check even when the recorded sid text
    happens to still read the same, because `_claim_dir_identity` is
    replaced here between the gate-time and recheck-time call."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "h9.md", holder="sid-dead-holder")
    _write_ledger_claim(repo, "h9.md", "sid-dead-holder")
    monkeypatch.chdir(repo)

    monkeypatch.setattr(
        pa_apply._liveness,
        "abandonment_basis",
        lambda sid, cwd=None: _V(True, "archive-record", False),
    )

    identities = iter([("sid-dead-holder", 1000.0), ("sid-dead-holder", 2000.0)])
    monkeypatch.setattr(pa_apply, "_claim_dir_identity", lambda claim_dir: next(identities))

    before_bytes = handoff.read_bytes()

    exit_code, report = pa_apply.drop(
        "state/handoffs/h9.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_CLAIM_DENIED
    assert report["reason"] == "drop_holder_resurrected"
    assert handoff.read_bytes() == before_bytes


def test_abandoned_release_reports_residue_via_shared_seam(tmp_path, monkeypatch):
    """Staff-eng review finding 3 — "the holder is dead" and "the work is
    free" are two verdicts, not one
    (state/lessons/2026-08-26-liveness-has-three-answers-not-two-and-m-
    23bdebd1994e.yaml). The dead holder's residue is reported through the
    EXISTING `claims._warn_dead_holder_residue` seam, not a third reader,
    and the release proceeds regardless of what that seam does (fail-open,
    advisory only)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_claimed_handoff(repo, "h10.md", holder="sid-dead-holder")
    _write_ledger_claim(repo, "h10.md", "sid-dead-holder")
    monkeypatch.chdir(repo)

    monkeypatch.setattr(
        pa_apply._liveness,
        "abandonment_basis",
        lambda sid, cwd=None: _V(True, "archive-record", False),
    )

    calls = []

    def _fake_warn(class_, basename, held_sid, cwd=None):
        calls.append((class_, basename, held_sid))

    monkeypatch.setattr(pa_apply, "_warn_dead_holder_residue", _fake_warn)

    exit_code, report = pa_apply.drop(
        "state/handoffs/h10.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK
    assert calls == [("handoff", "h10.md", "sid-dead-holder")]


def test_drop_reads_session_live_once_per_moment(tmp_path, monkeypatch):
    """Gate and recheck are each ONE verdict read: two `session_live` calls
    for the whole drop, not four."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_claimed_handoff(repo, "h11.md", holder="sid-dead-holder")
    _write_ledger_claim(repo, "h11.md", "sid-dead-holder")
    archive = repo / ".git" / "coordinator-sessions" / ".archive" / "sid-dead-holder-2026-01-01"
    archive.mkdir(parents=True)
    monkeypatch.chdir(repo)

    calls = []
    monkeypatch.setattr(
        pa_apply._liveness, "session_live", lambda sid, cwd=None: calls.append(sid) or False
    )

    exit_code, report = pa_apply.drop(
        "state/handoffs/h11.md", session_id="sid-not-holder", repo_root=repo
    )

    assert exit_code == pa_apply.APPLY_EXIT_OK, report
    assert report["abandonment_basis"] == "archive-record"
    assert calls == ["sid-dead-holder", "sid-dead-holder"]
    assert len(calls) == 2

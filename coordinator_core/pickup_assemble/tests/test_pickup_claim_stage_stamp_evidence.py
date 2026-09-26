from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.claim_state as claim_state_mod
import coordinator_core.pickup_brief as pa
from coordinator_core.session import claims as claims_mod
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness as liveness_mod

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


@pytest.fixture(autouse=True)
def _reset_registry_snapshot_cache():
    # first populates it (including one in a DIFFERENT file) leaks its
    liveness_mod._registry_snapshot_cache = None
    yield
    liveness_mod._registry_snapshot_cache = None


from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)


def _seed_handoff(repo: Path, name: str, *, claimed_by: str | None = None) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: active\n"
    )
    if claimed_by:
        fm += f"claimed_by: {claimed_by}\nclaimed_at: 2026-01-01T00:00:00Z\n"
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _claim_dir(repo: Path, basename: str) -> Path:
    return repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename


def _write_claim(
    repo: Path, basename: str, holder_sid: str, *, stage: str, stamped: bool = False
) -> Path:
    cdir = _claim_dir(repo, basename)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "pid").write_text("4242\n", encoding="utf-8")
    (cdir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    claimed = datetime.fromtimestamp(session_core.now_epoch(), tz=timezone.utc)
    (cdir / "claimed_at").write_text(
        claimed.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
    )
    (cdir / "stage").write_text(f"{stage}\n", encoding="utf-8")
    if stamped:
        (cdir / "stamped").write_text(
            claimed.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8"
        )
    return cdir


@pytest.fixture
def as_session(monkeypatch):
    def _bind(sid: str) -> None:
        monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    return _bind


@pytest.fixture
def holder_reads_live(monkeypatch):
    def _set(value: bool) -> None:
        monkeypatch.setattr(liveness_mod, "claim_holder_live", lambda *a, **k: value)

    return _set


def _d2(result) -> dict:
    for d in result.decision_object["directives"]:
        if d["id"] == "d2":
            return d
    raise AssertionError("d2 not found in directives")


def test_reclaim_basis_downgrades_recency_only_evidence_to_liveness_unknown(
    tmp_path, as_session, holder_reads_live, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "h1.md", "sid-old", stage="apply")
    as_session("sid-new")
    holder_reads_live(False)
    monkeypatch.setattr(
        liveness_mod, "session_verdict", lambda *a, **k: (False, "recency-window", 5400)
    )

    record = pa.acquire_brief_claim(repo, "handoff", "h1.md")

    assert record is not None
    assert record["holder"] == "sid-old"
    assert record["basis"] == "holder-liveness-unknown"
    assert record["liveness_basis"] == "recency-window"


def test_reclaim_basis_stays_dead_holder_for_confirmed_dead_process(
    tmp_path, as_session, holder_reads_live, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "h1.md", "sid-old", stage="apply")
    as_session("sid-new")
    holder_reads_live(False)
    monkeypatch.setattr(
        liveness_mod, "session_verdict", lambda *a, **k: (False, "stable-pid", None)
    )

    record = pa.acquire_brief_claim(repo, "handoff", "h1.md")

    assert record is not None
    assert record["holder"] == "sid-old"
    assert record["basis"] == "dead-holder"
    assert record["liveness_basis"] == "stable-pid"


def test_reclaim_basis_downgrades_confirmed_live_verdict_to_liveness_unknown(
    tmp_path, as_session, holder_reads_live, monkeypatch
):
    """`basis` used to be derived from
    `session_verdict`'s basis string alone, discarding the liveness boolean
    (slot 0). A `(True, "stable-pid", None)` verdict — the process-identity
    check CONFIRMED the holder alive — must never be labelled `"dead-holder"`
    just because the takeover itself fired on `claim_holder_live` (a
    separate, structurally-different computation — reachable via
    `COORDINATOR_SESSION_LAYER1_DISABLE`, which `session_live` honours and
    `session_verdict`/`_verdict_for_sdir` deliberately do not)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "h1.md", "sid-old", stage="apply")
    as_session("sid-new")
    holder_reads_live(False)
    monkeypatch.setattr(
        liveness_mod, "session_verdict", lambda *a, **k: (True, "stable-pid", None)
    )

    record = pa.acquire_brief_claim(repo, "handoff", "h1.md")

    assert record is not None
    assert record["holder"] == "sid-old"
    assert record["basis"] == "holder-liveness-unknown"
    assert record["liveness_basis"] == "stable-pid"


def test_reclaim_basis_holder_absent_for_no_evidence_at_all(
    tmp_path, as_session, holder_reads_live
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    _write_claim(repo, "h1.md", "sid-gone", stage="apply")
    as_session("sid-new")
    holder_reads_live(False)

    record = pa.acquire_brief_claim(repo, "handoff", "h1.md")

    assert record is not None
    assert record["holder"] == "sid-gone"
    assert record["basis"] == "holder-absent"
    assert record["liveness_basis"] is None


def test_fresh_pickup_brief_stage_claim_does_not_satisfy_d2(tmp_path, as_session):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=True)

    cdir = _claim_dir(repo, "h1.md")
    assert claims_mod.claim_stage(cdir) == claims_mod.CLAIM_STAGE_BRIEF
    assert _d2(result)["already_satisfied"] is False


def test_self_held_apply_stage_ledger_claim_with_empty_mirror_satisfies_d2(
    tmp_path, as_session, holder_reads_live
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)
    _write_claim(repo, "h1.md", "sid-a", stage="apply", stamped=True)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=False)

    assert _d2(result)["already_satisfied"] is True


def test_apply_stage_claim_with_refused_stamp_does_not_satisfy_d2(
    tmp_path, as_session, holder_reads_live
):
    """THE regression this module was repaired for (cross-repo/inbox/
    2026-08-13-doe-claude-em-pickup-already-satisfied-masks-a-refused-write.md):
    `apply.py::apply` promotes the claim dir to `apply` stage UNCONDITIONALLY,
    before `d2`'s directive (`archive-stamp-cli claim-handoff`) ever runs — so
    an `apply`-stage claim dir is reachable on a REFUSED stamp attempt exactly
    as much as on a landed one. No `stamped` marker present means no stamp
    ever landed; `d2` must NOT be reported `already_satisfied` on the next
    `brief`, even though the claim dir already reads `apply` stage.

    This is the case that FAILED before the fix (the old fallback read only
    `claim_stage(...) == CLAIM_STAGE_APPLY`, which is True here) and PASSES
    after it (the new fallback reads `claim_stamped(...)`, which is False
    here)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)
    _write_claim(repo, "h1.md", "sid-a", stage="apply", stamped=False)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=False)

    assert claims_mod.claim_stage(_claim_dir(repo, "h1.md")) == claims_mod.CLAIM_STAGE_APPLY
    assert _d2(result)["already_satisfied"] is False


def test_mirror_only_evidence_satisfies_d2(tmp_path, as_session, monkeypatch):
    """A mirror-sourced holder (frontmatter `claimed_by` present, ledger
    claim dead so `resolve_claim_state` degrades it to no ledger claim) plus
    `held_by_self` (an IDENTITY match, independent of liveness — see
    `session.liveness.claim_held_by_me`) is already-landed frontmatter — no
    ledger stage concept applies, and it must satisfy `d2`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md", claimed_by="sid-a")
    as_session("sid-a")
    _write_claim(repo, "h1.md", "sid-a", stage="apply")
    monkeypatch.setattr(claim_state_mod, "cs_claim_holder_live", lambda *a, **k: False)

    result = pa.brief("state/handoffs/h1.md", repo_root=repo, claim_at_brief=False)

    assert _d2(result)["already_satisfied"] is True


import coordinator_core.pickup_assemble.apply as apply_mod
from coordinator_core.claim_state import ClaimComparisonReport


def test_apply_ok_exit_prints_an_unqualified_agree_verdict(
    tmp_path, as_session, holder_reads_live, capsys
):
    """An `agree` verdict with no AC12 forward instrumentation on disk yet
    (every row filed before C10 lands, including this one) is unqualified —
    `ledger_resolver_source` degrades to `not-recorded` — so AC6/AC8 require
    it print, not stay silent, on the ordinary `APPLY_EXIT_OK` happy path."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)

    exit_code, report = apply_mod.apply(
        "state/handoffs/h1.md", session_id="sid-a", repo_root=repo
    )

    assert exit_code == apply_mod.APPLY_EXIT_OK
    err = capsys.readouterr().err
    assert "claim-state comparator:" in err
    assert "verdict=agree" in err
    assert "ledger_resolver_source='not-recorded'" in err


def test_apply_never_changes_exit_code_or_report_on_a_disagreeing_verdict(
    tmp_path, as_session, holder_reads_live, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)

    monkeypatch.setattr(
        apply_mod,
        "compare_claim_state",
        lambda *a, **k: ClaimComparisonReport(
            verdict="holder-mismatch",
            ledger_holder="sid-a",
            mirror_holder="sid-other",
            age=12.0,
            ledger_resolver_source="not-recorded",
            bound_seconds=300.0,
            bound_exceeded=False,
        ),
    )

    exit_code, report = apply_mod.apply(
        "state/handoffs/h1.md", session_id="sid-a", repo_root=repo
    )

    assert exit_code == apply_mod.APPLY_EXIT_OK
    assert "landed" in report
    err = capsys.readouterr().err
    assert "verdict=holder-mismatch" in err
    assert "ledger_holder='sid-a'" in err
    assert "mirror_holder='sid-other'" in err


def test_apply_qualified_agree_is_silent(
    tmp_path, as_session, holder_reads_live, capsys, monkeypatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)

    monkeypatch.setattr(
        apply_mod,
        "compare_claim_state",
        lambda *a, **k: ClaimComparisonReport(
            verdict="agree",
            ledger_holder="sid-a",
            mirror_holder="sid-a",
            age=None,
            ledger_resolver_source="attributable_session_id:warm",
            bound_seconds=300.0,
            bound_exceeded=False,
        ),
    )

    exit_code, report = apply_mod.apply(
        "state/handoffs/h1.md", session_id="sid-a", repo_root=repo
    )

    assert exit_code == apply_mod.APPLY_EXIT_OK
    err = capsys.readouterr().err
    assert "claim-state comparator:" not in err


def test_apply_transport_fail_exit_still_calls_the_comparator(
    tmp_path, as_session, holder_reads_live, capsys, monkeypatch
):
    """AC8 names `APPLY_EXIT_TRANSPORT_FAIL` as one of the four gated exits —
    the highest-divergence window in the system, per the row's own body.
    Forcing `_execute_directives`'s reported exit_code to
    `APPLY_EXIT_TRANSPORT_FAIL` drives the SAME post-directive-return path
    this row instruments, keeping everything else (the ledger/mirror state
    the comparator reads) on the real, unmocked path."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "h1.md")
    as_session("sid-a")
    holder_reads_live(True)

    real_execute = apply_mod._execute_directives

    def _forced_transport_fail(*args, **kwargs):
        _exit_code, forced_report = real_execute(*args, **kwargs)
        return apply_mod.APPLY_EXIT_TRANSPORT_FAIL, forced_report

    monkeypatch.setattr(apply_mod, "_execute_directives", _forced_transport_fail)

    exit_code, report = apply_mod.apply(
        "state/handoffs/h1.md", session_id="sid-a", repo_root=repo
    )

    assert exit_code == apply_mod.APPLY_EXIT_TRANSPORT_FAIL
    err = capsys.readouterr().err
    assert "claim-state comparator:" in err

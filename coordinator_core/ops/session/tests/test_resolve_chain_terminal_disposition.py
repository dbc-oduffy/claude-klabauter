"""Tests for coordinator_core.ops.session.resolve_chain_terminal_disposition.

Covers the B4 settlement contract (docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § B4):
  - 5-way session-id resolution superset (param > em_sid > CLAUDE_SESSION_ID >
    CLAUDE_CODE_SESSION_ID > sentinel > epoch fallback).
  - Primary detector: live state/handoffs/ claim stamp (accessor dual-read —
    new AND legacy vocabulary stamps both detected).
  - Detector A: archived claim stamp + predecessor well-formedness guard.
  - Detector B: git-provenance Session-Id trailer over the origin/main
    merge-base window, with the foreign-consumer restoration-commit spoof guard.
  - Locked disposition vocabulary: open/claimed/continued/closed only;
    banned legacy deployment token → CC-7 structured error, never re-mapped.
  - Double invocation (CC-4/AC7): pure read — identical second result.
  - Setup errors: repo_root None, D3 mismatch, non-string session_id.

All git activity is confined to the session_repo tmp_path fixture repo — no
mutation of the working repository (conftest.py owns the throwaway git init).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.session.resolve_chain_terminal_disposition import (
    _handler,
    _resolve_session_id,
)

_SID = "sess-b4-primary"
_OTHER_SID = "sess-b4-foreign"

# The three env tiers the resolver consults (plus em_sid) — cleared per-test so
# the harness's own session identity can never leak into a classification.
_ENV_TIERS = ("em_sid", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


@pytest.fixture(autouse=True)
def _clean_session_env(monkeypatch):
    for var in _ENV_TIERS:
        monkeypatch.delenv(var, raising=False)


def _invoke(session_repo, params: dict | None = None) -> dict:
    return asyncio.run(_handler(params or {}, repo_root=session_repo.common_dir))


def _git(session_repo, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(session_repo.root),
        capture_output=True,
        check=True,
    )


def _write_handoff(
    root: Path,
    relpath: str,
    *,
    claim_field: str = "claimed_by",
    claim_value: str = _SID,
    predecessor: str | None = "none",
    deployment_state: str | None = None,
    closed_reason: str | None = None,
) -> Path:
    """Fixture builder — writes a specific claim-stamp vocabulary as test DATA
    (INPUT-not-READ; production reads route through the shared accessor)."""
    lines = ["---", "title: fixture handoff", f"{claim_field}: {claim_value}"]
    if predecessor is not None:
        lines.append(f"predecessor: {predecessor}")
    if deployment_state is not None:
        lines.append(f"deployment_state: {deployment_state}")
    if closed_reason is not None:
        lines.append(f"closed_reason: {closed_reason}")
    lines += ["---", "", "body", ""]
    path = root / Path(relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Primary detector — live claim stamp
# ---------------------------------------------------------------------------


def test_live_claim_stamp_is_claimed_chain_terminal(session_repo):
    _write_handoff(session_repo.root, "state/handoffs/2026-07-22_a.md")

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["exit_code"] == 0
    assert result["disposition"] == "claimed"
    assert result["chain_terminal"] is True
    assert result["evidence"]["detector"] == "live_claim_stamp"
    assert result["evidence"]["consumed_handoff"] == "state/handoffs/2026-07-22_a.md"
    assert result["evidence"]["session_id"] == _SID
    assert result["evidence"]["session_id_source"] == "param"


def test_live_legacy_vocabulary_stamp_detected_via_accessor(session_repo):
    # DR-084 migration window: an old-vocabulary consume stamp must still be
    # detected — the shared accessor's dual-read, not a raw single-name read.
    _write_handoff(
        session_repo.root,
        "state/handoffs/2026-07-22_legacy.md",
        claim_field="consumed_by",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["disposition"] == "claimed"
    assert result["chain_terminal"] is True
    assert result["evidence"]["detector"] == "live_claim_stamp"


def test_foreign_live_stamp_is_not_a_hit(session_repo):
    _write_handoff(
        session_repo.root, "state/handoffs/2026-07-22_x.md", claim_value=_OTHER_SID
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["disposition"] == "open"
    assert result["chain_terminal"] is False
    assert result["evidence"]["detector"] is None


def test_no_handoffs_at_all_is_open_single_session(session_repo):
    result = _invoke(session_repo, {"session_id": _SID})

    assert result["exit_code"] == 0
    assert result["disposition"] == "open"
    assert result["chain_terminal"] is False
    assert result["evidence"]["consumed_handoff"] is None


# ---------------------------------------------------------------------------
# Detector A — archived claim stamp
# ---------------------------------------------------------------------------


def test_archived_shipped_handoff_is_claimed(session_repo):
    _write_handoff(
        session_repo.root,
        "archive/handoffs/2026-07/2026-07-20_done.md",
        deployment_state="shipped",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["disposition"] == "claimed"
    assert result["chain_terminal"] is True
    assert result["evidence"]["detector"] == "archive_claim_stamp"
    assert result["evidence"]["deployment_state"] == "shipped"
    assert (
        result["evidence"]["consumed_handoff"]
        == "archive/handoffs/2026-07/2026-07-20_done.md"
    )


def test_archived_continued_handoff_refines_to_continued(session_repo):
    _write_handoff(
        session_repo.root,
        "archive/handoffs/2026-07-20_cont.md",
        deployment_state="continued",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["disposition"] == "continued"
    assert result["chain_terminal"] is True


def test_archived_closed_handoff_surfaces_closed_reason(session_repo):
    _write_handoff(
        session_repo.root,
        "archive/handoffs/2026-07-20_closed.md",
        deployment_state="closed",
        closed_reason="stale",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["disposition"] == "closed"
    assert result["chain_terminal"] is True
    assert result["evidence"]["closed_reason"] == "stale"


def test_archived_banned_legacy_deployment_token_is_cc7_error(session_repo):
    _write_handoff(
        session_repo.root,
        "archive/handoffs/2026-07-20_legacy.md",
        deployment_state="abandoned",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["exit_code"] == 1
    assert result["disposition"] is None
    assert "abandoned" in result["error"]
    assert "2026-07-20_legacy.md" in result["error"]


def test_archived_unknown_closed_reason_is_cc7_error(session_repo):
    _write_handoff(
        session_repo.root,
        "archive/handoffs/2026-07-20_badreason.md",
        deployment_state="closed",
        closed_reason="superseded",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["exit_code"] == 1
    assert "superseded" in result["error"]


def test_archived_hit_without_predecessor_field_is_not_well_formed(session_repo):
    _write_handoff(
        session_repo.root,
        "archive/handoffs/2026-07-20_malformed.md",
        predecessor=None,
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["disposition"] == "open"
    assert result["chain_terminal"] is False


def test_live_stamp_wins_over_archive_detectors(session_repo):
    _write_handoff(session_repo.root, "state/handoffs/2026-07-22_live.md")
    _write_handoff(
        session_repo.root,
        "archive/handoffs/2026-07-20_arch.md",
        deployment_state="continued",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["disposition"] == "claimed"
    assert result["evidence"]["detector"] == "live_claim_stamp"


# ---------------------------------------------------------------------------
# Detector B — git provenance + spoof guard
# ---------------------------------------------------------------------------


def _commit_archived_handoff(session_repo, path: Path, trailer_sid: str) -> None:
    _git(session_repo, "add", str(path.relative_to(session_repo.root)))
    _git(
        session_repo,
        "commit",
        "-m",
        f"chore: ship handoff\n\nSession-Id: {trailer_sid}",
    )


def test_git_provenance_detects_shipped_handoff(session_repo):
    # The B-only escape shape: an archived handoff carrying NO claim stamp at
    # all (unconsume/repark churn, awaiting_gate/direct ship), reachable only
    # via the archiving commit's Session-Id trailer.  Claim-stamped archived
    # records are always Detector-A hits (same tree, A runs first) — B's
    # distinct value is exactly this no-stamp case, where the spoof guard's
    # accessor read resolves empty and passes.
    _git(session_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    unclaimed = session_repo.root / "archive" / "handoffs" / "2026-07-22_noclaim.md"
    unclaimed.parent.mkdir(parents=True, exist_ok=True)
    unclaimed.write_text(
        "---\ntitle: fixture\npredecessor: none\ndeployment_state: shipped\n---\n",
        encoding="utf-8",
    )
    _git(session_repo, "add", "-A")
    _git(
        session_repo,
        "commit",
        "-m",
        f"chore: ship handoff without live consume\n\nSession-Id: {_SID}",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["exit_code"] == 0
    assert result["disposition"] == "claimed"
    assert result["chain_terminal"] is True
    assert result["evidence"]["detector"] == "git_provenance"
    assert (
        result["evidence"]["consumed_handoff"]
        == "archive/handoffs/2026-07-22_noclaim.md"
    )


def test_foreign_consumer_spoof_guard_rejects_restoration_commit(session_repo):
    _git(session_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    # A restoration commit re-ADDs ANOTHER session's archived handoff under
    # THIS session's trailer — the 2026-07-22 incident shape.
    path = _write_handoff(
        session_repo.root,
        "archive/handoffs/2026-07-21_foreign.md",
        claim_value=_OTHER_SID,
        deployment_state="shipped",
    )
    _commit_archived_handoff(session_repo, path, _SID)

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["exit_code"] == 0
    assert result["disposition"] == "open"
    assert result["chain_terminal"] is False
    notes = "\n".join(result["evidence"]["notes"])
    assert "spoof guard" in notes
    # Fence-parity WARN: B saw an archived handoff but disposition resolved
    # single-session — surfaced loudly, never silently.
    warnings = "\n".join(result["evidence"]["warnings"])
    assert "single-session" in warnings


def test_own_claim_stamp_via_git_provenance_still_resolves_predecessor_consumed(
    session_repo,
):
    # Regression guard (2026-08-05 chain-terminal misattribution incident):
    # this is the point of the exercise — a session that GENUINELY archived
    # its own predecessor, with its own claim stamp on the archived record,
    # must keep resolving as predecessor-consumed after both defect fixes
    # below. An archived record carrying this session's own claim stamp is
    # ordinarily a Detector-A hit (A runs before B and wins) — asserted via
    # `detector in (archive_claim_stamp, git_provenance)` below rather than
    # pinning to one, since the point is the end-to-end disposition survives
    # the tightened ownership guard, not which detector supplies it.
    _git(session_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    path = _write_handoff(
        session_repo.root,
        "archive/handoffs/2026-08/2026-08-04_own.md",
        claim_value=_SID,
        deployment_state="shipped",
    )
    _commit_archived_handoff(session_repo, path, _SID)

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["exit_code"] == 0
    assert result["disposition"] == "claimed"
    assert result["chain_terminal"] is True
    # Detector A already finds this (own claim stamp, archived) — asserting
    # the shape holds end to end, not asserting which detector fired.
    assert result["evidence"]["detector"] in ("archive_claim_stamp", "git_provenance")


def test_absent_claim_holder_with_contradicting_origin_session_is_rejected(
    session_repo,
):
    # Live reproduction (2026-08-05): a session.boot_sweep run nested inside
    # THIS session archived ANOTHER session's handoff (commit `d63b5d5df`,
    # subject "fleet: archive 1 completed handoff(s)"). The archived record
    # carried no claim holder at all AND an origin_session naming a
    # different session — the guard must reject this, not fall through to
    # acceptance because `consumer` was falsy. Built from the actual field
    # shapes: sweep-shaped commit subject (Defect 2's gate) STACKED with
    # absent-claim-holder + contradicting origin_session (Defect 1's guard)
    # — either alone would already reject this candidate; the real incident
    # carried both.
    _git(session_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    foreign_origin = "sess-56449a69-9d81-4044-aa9f-8e48115f7dda"
    path = session_repo.root / "archive" / "handoffs" / "2026-07" / "2026-07-10_roadmap-qsub-03.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "title: fixture\n"
        "predecessor: none\n"
        "deployment_state: shipped\n"
        f"origin_session: {foreign_origin}\n"
        "---\n",
        encoding="utf-8",
    )
    _git(session_repo, "add", "-A")
    _git(
        session_repo,
        "commit",
        "-m",
        f"fleet: archive 1 completed handoff(s)\n\nSession-Id: {_SID}",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["exit_code"] == 0
    assert result["disposition"] == "open"
    assert result["chain_terminal"] is False
    # Defect 2's sweep-subject gate fires first (cheapest, most decisive —
    # see _git_provenance_shipped_handoff) and rejects before Defect 1's
    # origin_session check is even reached for THIS candidate; the isolated
    # defect-1-only test below exercises the origin_session branch directly
    # with a non-sweep-shaped commit.
    notes = "\n".join(result["evidence"]["notes"])
    assert "fleet: archive 1 completed handoff(s)" in notes


def test_absent_claim_holder_with_contradicting_origin_session_is_rejected_alone(
    session_repo,
):
    # Isolated Defect 1 coverage: NON-sweep-shaped commit (so Defect 2's gate
    # cannot be why this is rejected), absent claim holder, origin_session
    # naming a different session — the guard must still reject on the
    # origin_session signal alone.
    _git(session_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    foreign_origin = "sess-56449a69-9d81-4044-aa9f-8e48115f7dda"
    path = session_repo.root / "archive" / "handoffs" / "2026-08-05_isolated.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "title: fixture\n"
        "predecessor: none\n"
        "deployment_state: shipped\n"
        f"origin_session: {foreign_origin}\n"
        "---\n",
        encoding="utf-8",
    )
    _git(session_repo, "add", "-A")
    _git(
        session_repo,
        "commit",
        "-m",
        f"chore: ship handoff without live consume\n\nSession-Id: {_SID}",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["exit_code"] == 0
    assert result["disposition"] == "open"
    assert result["chain_terminal"] is False
    notes = "\n".join(result["evidence"]["notes"])
    assert "origin_session" in notes
    assert foreign_origin in notes


def test_sweep_shaped_commit_not_counted_even_with_plausible_other_fields(
    session_repo,
):
    # Defect 2: a bulk/housekeeping archival commit (`fleet: archive …`) is
    # never consumption evidence, independent of Defect 1's ownership check
    # — this fixture deliberately carries NO claim holder and NO
    # origin_session at all (the "plausible looking" shape that Defect 1
    # alone would accept, per the regression-guard test above), so only the
    # sweep-shaped subject can be rejecting it.
    _git(session_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    path = session_repo.root / "archive" / "handoffs" / "2026-08-05_swept.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntitle: fixture\npredecessor: none\ndeployment_state: shipped\n---\n",
        encoding="utf-8",
    )
    _git(session_repo, "add", "-A")
    _git(
        session_repo,
        "commit",
        "-m",
        f"fleet: archive 1 completed handoff(s)\n\nSession-Id: {_SID}",
    )

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["exit_code"] == 0
    assert result["disposition"] == "open"
    assert result["chain_terminal"] is False
    notes = "\n".join(result["evidence"]["notes"])
    assert "fleet: archive 1 completed handoff(s)" in notes


def test_missing_origin_main_skips_detector_b_with_warning(session_repo):
    # The fixture repo has no origin/main ref by default.
    result = _invoke(session_repo, {"session_id": _SID})

    assert result["disposition"] == "open"
    warnings = "\n".join(result["evidence"]["warnings"])
    assert "merge-base" in warnings


# ---------------------------------------------------------------------------
# Session-id resolution chain
# ---------------------------------------------------------------------------


def test_param_session_id_outranks_all_env_tiers(session_repo, monkeypatch):
    monkeypatch.setenv("em_sid", "env-em")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-cs")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-ccs")

    result = _invoke(session_repo, {"session_id": _SID})

    assert result["evidence"]["session_id"] == _SID
    assert result["evidence"]["session_id_source"] == "param"


def test_em_sid_outranks_claude_session_id(session_repo, monkeypatch):
    monkeypatch.setenv("em_sid", "env-em")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-cs")

    result = _invoke(session_repo)

    assert result["evidence"]["session_id"] == "env-em"
    assert result["evidence"]["session_id_source"] == "em_sid"


def test_claude_session_id_outranks_claude_code_session_id(session_repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-cs")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-ccs")

    result = _invoke(session_repo)

    assert result["evidence"]["session_id"] == "env-cs"
    assert result["evidence"]["session_id_source"] == "CLAUDE_SESSION_ID"


def test_sentinel_fallback(session_repo):
    sentinel = session_repo.sessions_dir / ".current-session-id"
    sentinel.write_text(f"{_SID}\n", encoding="utf-8")

    result = _invoke(session_repo)

    assert result["evidence"]["session_id"] == _SID
    assert result["evidence"]["session_id_source"] == "sentinel"


def test_epoch_fallback_is_six_digits(session_repo):
    result = _invoke(session_repo)

    sid = result["evidence"]["session_id"]
    assert result["evidence"]["session_id_source"] == "epoch_fallback"
    assert len(sid) == 6
    assert sid.isdigit()


def test_resolve_session_id_pure_chain(tmp_path):
    # Direct unit coverage of the resolver's tier ordering (no repo needed).
    sid, source = _resolve_session_id("explicit", tmp_path, {"em_sid": "shadowed"})
    assert (sid, source) == ("explicit", "param")
    sid, source = _resolve_session_id(None, tmp_path, {"CLAUDE_CODE_SESSION_ID": "ccs"})
    assert (sid, source) == ("ccs", "CLAUDE_CODE_SESSION_ID")


# ---------------------------------------------------------------------------
# CC-4 / AC7: double invocation — pure read, identical classification
# ---------------------------------------------------------------------------


def test_double_invocation_is_identical(session_repo):
    _write_handoff(session_repo.root, "state/handoffs/2026-07-22_idem.md")

    first = _invoke(session_repo, {"session_id": _SID})
    second = _invoke(session_repo, {"session_id": _SID})

    assert first == second
    assert second["disposition"] == "claimed"


def test_double_invocation_open_case_is_identical(session_repo):
    first = _invoke(session_repo, {"session_id": _SID})
    second = _invoke(session_repo, {"session_id": _SID})

    assert first == second
    assert second["disposition"] == "open"


# ---------------------------------------------------------------------------
# Setup errors
# ---------------------------------------------------------------------------


def test_repo_root_none_is_setup_error():
    result = asyncio.run(_handler({}, repo_root=None))
    assert result["exit_code"] == 1
    assert result["disposition"] is None
    assert "repo_root" in result["error"]


def test_d3_mismatch_is_setup_error(session_repo, tmp_path):
    other = tmp_path / "unrelated"
    other.mkdir()

    result = _invoke(session_repo, {"repo_root": str(other)})

    assert result["exit_code"] == 1
    assert result["disposition"] is None


def test_non_string_session_id_is_setup_error(session_repo):
    result = _invoke(session_repo, {"session_id": 12345})

    assert result["exit_code"] == 1
    assert "session_id" in result["error"]

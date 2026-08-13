"""Regression coverage for the 2026-08-13 code-review Slice-3 P2 finding:
`_ledger_claim_record`'s ledger-only, liveness-free read
(`coordinator_core.claim_state.resolve_historical_claim`) rested on an
UNENFORCED prose invariant -- "the only predecessor that ever reaches a
supersede is one a session claimed, worked, and exited" -- with nothing
structural named as the enforcer.

STEP 1 VERDICT (verified by execution, not inference): reachability of "a
predecessor whose true ledger holder is a DIFFERENT session that is STILL
LIVE" into this liveness-free accessor is FALSE. Two probes settled it:

  Probe 1 (dead holder -- a ledger record naming a session id with no real
  session dir, hence naturally dead): reached `_reconcile_claim_from_ledger`
  and reconciled -- the documented, intended incident.

  Probe 2 (the SAME fixture, with `coordinator_core.session.liveness.
  claim_holder_live` forced to report that record's holder as LIVE):
  `_dispatch_handoff_supersede_predecessor`'s own DR-242 gate
  (`archival.claimed_or_shipped_at_path`) already reported the predecessor
  claimed-or-shipped BEFORE `_reconcile_claim_from_ledger` was ever called --
  that gate independently consults `claim_state.resolve_claim_state`, whose
  own `cs_claim_holder_live` check is what actually determines whether a
  live ledger claim counts. The op composed directly off that gate's own
  live-claim read; `_reconcile_claim_from_ledger`'s reconcile print never
  fired and the frontmatter was left untouched by it (a real
  `handoff.archive_transition` supersede would still stamp the predecessor
  through its own writer -- irrelevant to what this file pins, which is
  whether the LIVENESS-FREE accessor itself is reached).

CONCLUSION: the retired prose invariant is now structurally enforced by
`_dispatch_handoff_supersede_predecessor`'s call ORDER -- the liveness-aware
DR-242 gate always runs first, and the liveness-free ledger read
(`_ledger_claim_record` / `_reconcile_claim_from_ledger`) is reachable ONLY
on the branch where that gate already found no live claim. This file pins
that ordering property so a future refactor that reaches the liveness-free
read on a genuinely-live different holder (e.g. by reordering the DR-242
gate after the reconcile call, or by ever calling
`_reconcile_claim_from_ledger` unconditionally) fails loudly here.

No production code changed for this finding -- see
`_ledger_claim_record`'s own "REACHABILITY" docstring paragraph in
`coordinator_core/baton_assemble/apply.py`, which now names this ordering
as the enforcing mechanism in place of the old bare prose assertion.
"""

from __future__ import annotations

from pathlib import Path

import coordinator_core.baton_assemble.apply as ba_apply
import coordinator_core.session.liveness as session_liveness
from coordinator_core.ops.fleet._common import handoff_claim_dir
from coordinator_core.test_baton_assemble import _git, _init_repo, _write_artifact

_PRED_REL = "state/handoffs/predecessor.md"

_UNCLAIMED_PREDECESSOR_FM = [
    "handoff_id: hnd-pred-1a2b4c",
    "status: open",
    "deployment_state: ready_to_fire",
    "title: Predecessor handoff",
    "created: 2026-07-27",
    "branch: work/test/2026-01-01",
    'predecessor: "none"',
    "category: infra",
    "summary: predecessor whose claim lives only in the durable ledger",
    "pickup_ready: true",
]


def _seed_repo(repo: Path) -> Path:
    _init_repo(repo)
    predecessor = _write_artifact(repo / _PRED_REL, list(_UNCLAIMED_PREDECESSOR_FM))
    _git(repo, "add", _PRED_REL)
    _git(repo, "commit", "-m", "add predecessor")
    return predecessor


def _seed_ledger_claim(repo_root: Path, session_id: str, claimed_at: str) -> Path:
    claim_dir = handoff_claim_dir(repo_root / ".git", Path(_PRED_REL))
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    return claim_dir


def _stub_archive_transition(monkeypatch) -> list:
    """Fakes only `handoff.archive_transition` -- the claim re-stamp itself
    (when it runs at all) still routes through the real `handoff.transition`
    op, matching the established idiom in
    `TestSupersedeReconcilesClaimFromDurableLedger`
    (`coordinator_core/test_baton_assemble.py`)."""
    calls: list = []
    real_invoke = ba_apply._invoke_op_in_process

    def _routed(op_name, params, repo_root):
        if op_name == "handoff.archive_transition":
            calls.append(params)
            return {"exit_code": 0, "superseded": True, "moved": True}
        return real_invoke(op_name, params, repo_root)

    monkeypatch.setattr(ba_apply, "_invoke_op_in_process", _routed)
    return calls


class TestLivenessFreeReadIsReachableOnlyOnADeadOrMissingHolder:
    """Pins the STRUCTURAL property Step 1 found in place of the retired
    prose invariant: `_reconcile_claim_from_ledger` (and, through it,
    `_ledger_claim_record`'s liveness-free `resolve_historical_claim` read)
    is reached only on the branch where the DR-242 gate
    (`archival.claimed_or_shipped_at_path`) has ALREADY found no live
    claim -- never when that gate's own liveness-aware read
    (`claim_state.resolve_claim_state` / `cs_claim_holder_live`) reports the
    ledger holder live, same session or not."""

    def test_dead_ledger_holder_reaches_reconcile_and_is_stamped(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = tmp_path / "repo"
        predecessor = _seed_repo(repo)
        # No real session dir for this id -- naturally dead under the real,
        # unmocked liveness stack (mirrors the documented incident: a
        # session that claimed, worked, and exited).
        _seed_ledger_claim(repo, "sid-exited-holder", "2026-08-13T10:00:00Z")

        calls = _stub_archive_transition(monkeypatch)

        result = ba_apply._dispatch_handoff_supersede_predecessor(
            [_PRED_REL, "state/handoffs/successor.md", "state/handoffs/successor.md"], repo
        )

        assert result.get("degraded") is None
        assert len(calls) == 1
        text = predecessor.read_text(encoding="utf-8")
        assert "status: claimed" in text
        assert "claimed_by: sid-exited-holder" in text
        assert "reconciled" in capsys.readouterr().err

    def test_live_ledger_holder_is_caught_by_dr242_gate_before_reconcile_runs(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = tmp_path / "repo"
        predecessor = _seed_repo(repo)
        before = predecessor.read_text(encoding="utf-8")
        _seed_ledger_claim(repo, "sid-real-holder-still-live", "2026-08-13T10:00:00Z")

        # Force the SAME liveness primitive the DR-242 gate's own
        # `resolve_claim_state` consults to report this holder LIVE --
        # the reviewer's exact scenario.
        monkeypatch.setattr(
            session_liveness, "claim_holder_live", lambda cdir, cwd=None: True
        )

        from coordinator_core.archival import claimed_or_shipped_at_path

        # The DR-242 gate itself already sees this as claimed -- BEFORE
        # `_dispatch_handoff_supersede_predecessor` is even called.
        assert claimed_or_shipped_at_path(str(repo / _PRED_REL)) is True

        calls = _stub_archive_transition(monkeypatch)

        result = ba_apply._dispatch_handoff_supersede_predecessor(
            [_PRED_REL, "state/handoffs/successor.md", "state/handoffs/successor.md"], repo
        )

        assert result.get("degraded") is None
        assert len(calls) == 1
        # `_reconcile_claim_from_ledger` was never reached: its "reconciled"
        # print never fired, and the predecessor -- whose real stamp is the
        # `handoff.archive_transition` op's own job, faked away here -- is
        # untouched by THIS liveness-free path.
        assert "reconciled" not in capsys.readouterr().err
        assert predecessor.read_text(encoding="utf-8") == before


class TestExplicitArtifactPathDoesReachTheDR242Gate:
    """The OTHER half of the reviewer's concern -- confirmed TRUE by
    execution: `resolve_lineage`'s explicit-`artifact_path` route for
    `kind="handoff"` (a caller-supplied path that is itself a handoff
    record) does set `lineage["predecessor"]` to that foreign path
    unconditionally, with no filter to the calling session's own claims.
    This is the shape that makes `TestLivenessFreeReadIsReachableOnlyOnA
    DeadOrMissingHolder` above a live concern worth pinning, rather than an
    unreachable branch -- the gate above is what makes it SAFE, not the
    absence of the caller-supplied-foreign-path route."""

    def test_explicit_artifact_path_naming_a_foreign_predecessor_sets_lineage_predecessor(
        self, tmp_path, monkeypatch
    ):
        import coordinator_core.baton_assemble as ba

        repo = tmp_path / "repo"
        _seed_repo(repo)
        _seed_ledger_claim(repo, "sid-real-holder", "2026-08-13T10:00:00Z")

        monkeypatch.setattr(
            ba,
            "resolve_operator_config",
            lambda: {
                "settings_home": "/fake/settings-home",
                "claude_klabauter_bin": "/fake/settings-home/bin",
                "doe_root": "/fake/doe-root",
            },
        )

        lineage = ba.resolve_lineage("handoff", _PRED_REL, repo)

        assert lineage["predecessor"] == _PRED_REL

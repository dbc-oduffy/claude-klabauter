"""Closed registry of every reason the commit and land path refuses, withholds or parks, each
with the irreversible harm it prevents or an engine-bug mark (north star: a reason that denies
recoverable work is an engine bug).

A key is the reason string the path emits (``refused=``, a ``withheld`` value, a park reason).
``harm`` is a ``deny_registry.IRREVERSIBLE_CLASSES`` member or None; ``engine_bug`` marks a
reason that denies recoverable work; ``owner`` names the sibling ask that already owns the fix.
``denies`` is False for a reason that is only recorded as provenance and never stops a land.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from coordinator_core.deny_registry import (
    BOX_DEGRADING,
    HEAD_INTEGRITY,
    IRREVERSIBLE_EXTERNAL,
    LANDING_REFUSAL_CLASS,
    MALFORMED_REQUEST,
)
from coordinator_core.ops.dispatch_emit.landing_refusal_kinds import PROVENANCE_KINDS

KINDS = frozenset({"refusal", "withhold", "park"})

#: Sibling asks that own a single class (R2).
SIBLING_OWNERS = frozenset({"3c236b-1", "f9b322", "17193e", "d0fa7d", "since-base-overlap"})

#: Row ids of ``state/bug-queue/2026-11-18-land-refusal-engine-bug-owners.yaml``; a test pins
#: this tuple equal to the file's rows.
QUEUE_ROWS = (
    "land-refusal-review-owed",
    "land-refusal-ledger-unmet",
    "land-refusal-no-change",
    "land-refusal-own-post-checkpoint-hunks-unverifiable",
    "land-refusal-own-post-checkpoint-hunks-unclaimed",
    "land-refusal-missing-run-outcome",
    "land-refusal-ledger-unjudged",
    "land-refusal-ledger-unreadable",
    "land-refusal-recovery-withheld-unbacked",
    "land-refusal-held-landed-overlap",
    "land-refusal-peer-claim",
    "land-refusal-receipt-write-failed",
    "land-refusal-verdict-pin-stale",
    "land-refusal-commit",
    "land-refusal-no-credited-chunk-landed",
    "land-refusal-own-hunk-stage-refused",
    "land-refusal-queued",
    "land-refusal-held-revision-unconsumed",
    "land-refusal-terminal-commit-not-landed",
    "land-refusal-commit-refused",
    "land-refusal-fanout-not-fired",
    "land-refusal-halt-record-only",
    "land-refusal-repo-not-opted-in",
    "land-refusal-halt-unowned-product",
    "land-refusal-scratch-export",
    "land-refusal-stopped-row",
    "land-refusal-split-chunk",
    "land-refusal-print-mismatch",
    "land-refusal-no-recorded-print",
    "land-refusal-not-ledger-check-halt",
    "land-refusal-run-dirt-uncommitted",
    "land-refusal-dependents-unstarted",
    "land-refusal-no-folded-stage-manifest",
)

#: A registry ``owner`` is a sibling ask or ``claude-klabauter:<queue row id>``.
OWNERS = SIBLING_OWNERS | frozenset(f"claude-klabauter:{row}" for row in QUEUE_ROWS)

_PKG = "coordinator_core.ops.dispatch_emit."


@dataclass(frozen=True)
class RefusalClass:
    """One refusal, withhold or park reason of the land leg."""

    kind: str
    harm: Optional[str]
    engine_bug: bool
    owner: Optional[str]
    emitted_by: str
    denies: bool = True
    note: str = ""


def _e(module: str, function: str) -> str:
    return f"{_PKG}{module}::{function}"


def _harm(kind: str, harm: str, emitted_by: str, note: str = "") -> RefusalClass:
    return RefusalClass(kind, harm, False, None, emitted_by, True, note)


def _q(kind: str) -> str:
    return f"claude-klabauter:land-refusal-{kind}"


def _bug(
    kind: str, emitted_by: str, owner: str, note: str = "", denies: bool = True
) -> RefusalClass:
    return RefusalClass(kind, None, True, owner, emitted_by, denies, note)


_TC = "terminal_commit"
_TCI = _e(_TC, "_terminal_commit_inner")
_LI = _e("land_run", "_land_inner")
_LEDGER = _e("terminal_commit_ledger", "_ledger_gate")
_FANOUT = "land_run_fanout"

_CLASSES: dict = {
    # -- LANDING_REFUSAL_KINDS: the closed table of reasons that may deny a landing.
    "destructive-git": _harm("refusal", HEAD_INTEGRITY, _e("landing_secrets", "destructive_refusal"),
                             "no LANDING_REFUSAL_CLASS row; the shape is rewriting or discarding history"),
    "secret-in-diff": _harm("refusal", IRREVERSIBLE_EXTERNAL, _e("landing_secrets", "refusal"),
                            "a landed secret reaches every consumer on publish; no LANDING_REFUSAL_CLASS row"),
    "lost-definition": _harm("refusal", LANDING_REFUSAL_CLASS["lost-definition"],
                             _e("terminal_commit_refusals", "_landing_integrity_gate")),
    # -- Terminal-commit refusals (terminal_commit_refusals._*_refusal and the inner gates).
    "tests-not-run": _bug("refusal", _e("terminal_commit_refusals", "_tests_not_run_refusal"), "f9b322",
                          "zero-run verify; also a provenance kind"),
    "review-owed": _bug("refusal", _e("terminal_commit_refusals", "_tests_not_run_refusal"), _q("review-owed")),
    "unreviewed": _harm("refusal", MALFORMED_REQUEST, _e("run_landing", "_review_floor"),
                        "refuses only a request carrying no review-stage output; a staged path in no "
                        "reviewed slice is also a provenance kind"),
    "ledger-unmet": _bug("refusal", _e("terminal_commit_refusals", "_measured_rev_drift_refusal"),
                         _q("ledger-unmet"), note="also _pre_existing_dirt_refusal and _ledger_gate; a met row refused on stale or foreign state"),
    "no-change": _bug("refusal", _e("terminal_commit_refusals", "_no_change_refusal"), _q("no-change")),
    "own-post-checkpoint-hunks-unverifiable": _bug(
        "refusal", _e("terminal_commit_refusals", "_snapshot_unavailable_refusal"),
        _q("own-post-checkpoint-hunks-unverifiable")),
    "own-post-checkpoint-hunks-unclaimed": _bug(
        "refusal", _e("terminal_commit_refusals", "_unclaimed_refusal"),
        _q("own-post-checkpoint-hunks-unclaimed")),
    "missing-run-outcome": _bug("refusal", _e("terminal_commit_refusals", "_missing_run_outcome"),
                                _q("missing-run-outcome")),
    "ledger-unjudged": _bug("refusal", _LEDGER, _q("ledger-unjudged")),
    "ledger-unreadable": _bug("refusal", _LEDGER, _q("ledger-unreadable")),
    "recovery-withheld-unbacked": _bug("refusal", _LEDGER, _q("recovery-withheld-unbacked"),
                                       note="also a provenance kind"),
    "halted-run": _harm("refusal", MALFORMED_REQUEST, _TCI,
                        "a halted run belongs to dispatch.land_run; also a provenance kind"),
    "held-landed-overlap": _bug("refusal", _TCI, _q("held-landed-overlap")),
    "peer-claim": _bug("refusal", _TCI, _q("peer-claim")),
    "declared-write-missing": _bug("refusal", _TCI, "3c236b-1", "a declared write absent from disk"),
    "receipt-write-failed": _bug("refusal", _TCI, _q("receipt-write-failed")),
    "head-moved": _harm("refusal", HEAD_INTEGRITY, _TCI),
    "branch-mismatch": _harm("refusal", HEAD_INTEGRITY, _TCI),
    "branch-unreadable": _harm("refusal", HEAD_INTEGRITY, _TCI),
    "detached-head": _harm("refusal", HEAD_INTEGRITY, _TCI),
    "run-token": _harm("refusal", HEAD_INTEGRITY, _e(_TC, "_handler"), "a stale token would land onto another run's head"),
    "run-token-in-flight": _harm("refusal", HEAD_INTEGRITY, _e(_TC, "_redeem_or_answer")),
    "run-token-adopted": _harm("refusal", HEAD_INTEGRITY, _LI),
    "malformed-request": _harm("refusal", MALFORMED_REQUEST, _TCI),
    "mode-ambiguous": _harm("refusal", MALFORMED_REQUEST, _TCI),
    "both-markers": _harm("refusal", MALFORMED_REQUEST, _TCI),
    "script-missing": _harm("refusal", MALFORMED_REQUEST, _TCI),
    "manifest-escapes-run-root": _harm("refusal", MALFORMED_REQUEST, _TCI),
    "empty-subject": _harm("refusal", MALFORMED_REQUEST, _TCI),
    # -- land_run refusals.
    "verdict-pin-stale": _bug("refusal", _LI, _q("verdict-pin-stale"), note="a reviewer-applied edit that passes reverify invalidates the pin; fixed by this audit (C3)"),
    "blob-drift": _harm("refusal", HEAD_INTEGRITY, _LI),
    "commit": _bug("refusal", _LI, _q("commit"), note="a commit failure passed through, not a rule"),
    "no-credited-chunk-landed": _bug("refusal", _LI, _q("no-credited-chunk-landed")),
    "own-hunk-head-moved": _harm("refusal", HEAD_INTEGRITY, _LI),
    "own-hunk-stage-refused": _bug("refusal", _LI, _q("own-hunk-stage-refused")),
    "commit-set-not-recorded-hunks": _harm("refusal", HEAD_INTEGRITY, _LI,
                                           "the commit would carry a hunk the run did not record or omit one it did; HEAD would hold a half-landed image"),
    "queued": _bug("refusal", _e("land_run", "_dispatch"), _q("queued"),
                   note="a serialization answer, not a verdict"),
    "held-revision-unconsumed": _bug("refusal", _e("land_run", "held_revision_refusal"),
                                     _q("held-revision-unconsumed")),
    "no-close-job": _harm("refusal", MALFORMED_REQUEST, _e("land_run", "_reclose")),
    "close-job-plan-mismatch": _harm("refusal", MALFORMED_REQUEST, _e("land_run", "_reclose")),
    "terminal-commit-not-landed": _bug("refusal", _e("land_run", "_reclose"),
                                       _q("terminal-commit-not-landed")),
    "run-id-unsafe": _harm("refusal", MALFORMED_REQUEST, _e("halted_record_commit", "_commit_halted_record")),
    "record-dir-escapes": _harm("refusal", MALFORMED_REQUEST, _e("halted_record_commit", "_land")),
    "halt-on-run-owned-merge": _harm("refusal", HEAD_INTEGRITY, _e("land_run_halt", "_run_owned_merge_reply"),
                                     "an open merge of the run's own would be committed half-resolved"),
    "commit-refused": _bug("refusal", _e(_FANOUT, "_land_and_fan_out"), _q("commit-refused")),
    "fanout-no-sizing": _harm("refusal", MALFORMED_REQUEST, _e(_FANOUT, "_resolve_plans")),
    "fanout-plan-path": _harm("refusal", MALFORMED_REQUEST, _e(_FANOUT, "_resolve_plans")),
    "fanout-plan-empty": _harm("refusal", MALFORMED_REQUEST, _e(_FANOUT, "_resolve_plans")),
    "fanout-waves": _harm("refusal", MALFORMED_REQUEST, _e(_FANOUT, "_plan_waves")),
    "fanout-parent-sizing": _harm("refusal", MALFORMED_REQUEST, _e(_FANOUT, "_parent_mode")),
    "fanout-not-fired": _bug("refusal", _e(_FANOUT, "_land_and_fan_out"), _q("fanout-not-fired")),
    "grind-params": _harm("refusal", MALFORMED_REQUEST, _e("land_run_grind", "land_green_rows")),
    # -- Gates that stop a land from carrying bytes or paths nobody verified or owns.
    "landing-tree-differs-from-measured": _harm("refusal", HEAD_INTEGRITY, _LI,
                                                "the landing tree is not the tree the verify measured"),
    "kept-blob-stale": _harm("refusal", HEAD_INTEGRITY, _TCI, "a kept blob no longer matches the worktree"),
    "post-commit-path-unlanded": _harm("refusal", HEAD_INTEGRITY, _TCI,
                                       "a declared path is absent from the commit just made"),
    "shared-path-sibling-held": _harm("refusal", HEAD_INTEGRITY, _LI, "a live sibling holds the shared path"),
    "needed-path-omitted": _harm("refusal", MALFORMED_REQUEST, _LI, "the request leaves out a path the land needs"),
    "subject-declared-write-unattributed": _harm("refusal", MALFORMED_REQUEST, _LI),
    "reach-claim-unreachable": _harm("refusal", MALFORMED_REQUEST, _LI),
    "credited-chunk-uncommitted": _harm("refusal", MALFORMED_REQUEST, _TCI),
    "credited-prefix-row-zero-paths": _harm("refusal", MALFORMED_REQUEST, _TCI),
    "pack-read-stall": _harm("refusal", BOX_DEGRADING, _TCI, "a stalled pack read would hold the box"),
    "verify-overlay-red": _harm("refusal", HEAD_INTEGRITY, _LI, "the clean overlay verify measured the landing red"),
    "verify-overlay-unverified": _harm("refusal", HEAD_INTEGRITY, _LI,
                                       "the clean overlay verify could not measure the landing"),
    "new-red": _harm("refusal", HEAD_INTEGRITY, _LI, "the landing adds a red test to HEAD"),
    "import-closure": _harm("refusal", HEAD_INTEGRITY, _e("land_run_grind", "_land"),
                            "the landing imports code HEAD will lack"),
    "ref-lock-held": _harm("refusal", HEAD_INTEGRITY, _e("run_landing", "_ref_lock_error"),
                           "a held ref lock is never broken to land"),
    "index-lock-busy": _harm("refusal", HEAD_INTEGRITY, _e("run_landing", "_commit"),
                             "a peer holds index.lock; its staging is never overwritten"),
    "head-unreadable": _harm("refusal", HEAD_INTEGRITY, _e("run_landing", "_stamp_commit"),
                             "a commit built on an unreadable HEAD tree would drop its files"),
    "dispatch-abandoned": _harm("refusal", HEAD_INTEGRITY, _e("run_landing", "_commit"),
                                "no ref moves after the dispatcher gave up on the op"),
    # -- Withholds (a path is left out; the land of the rest proceeds).
    "halt-record-only": _bug("withhold", _e("halted_record_commit", "_land"),
                             _q("halt-record-only"), denies=False),
    "peer-held": _harm("withhold", HEAD_INTEGRITY, _e("halted_record_commit", "_land"),
                       "a live peer's uncommitted bytes are never swept into this commit"),
    "peer-claim-indeterminate": _bug("withhold", _e("halted_record_commit", "_land"), "17193e",
                                     "withheld with no peer manifest claiming the path (451d84, 69ed2c)"),
    "sibling-claimed": _harm("withhold", HEAD_INTEGRITY, _e("halted_record_commit", "_land")),
    "foreign-halted-withheld": _harm("withhold", HEAD_INTEGRITY, _e("halted_record_commit", "_commit_halted_record")),
    "repo-not-opted-in": _bug("withhold", _e("process_artifact_policy", "withhold_process_artifacts"),
                              _q("repo-not-opted-in"), denies=False),
    "halt-unowned-product": _bug("withhold", _e("halt_product_gates", "gate_halt_product"),
                                 _q("halt-unowned-product"), denies=False),
    "scratch-export": _bug("withhold", _e("halted_record_commit", "_land"),
                           _q("scratch-export"), denies=False),
    "stopped-row": _bug("withhold", _e("halted_record_commit", "withheld_cause"),
                        _q("stopped-row"), denies=False),
    "split-chunk": _bug("withhold", _e("halted_record_commit", "withheld_cause"),
                        _q("split-chunk"), denies=False),
    "print-mismatch": _bug("withhold", _e("halted_record_commit", "withheld_cause"),
                           _q("print-mismatch"), denies=False),
    "no-recorded-print": _bug("withhold", _e("halted_record_commit", "withheld_cause"),
                              _q("no-recorded-print"), denies=False),
    "not-ledger-check-halt": _bug("withhold", _e("halted_record_commit", "withheld_cause"),
                                  _q("not-ledger-check-halt"), denies=False),
    "evicted-parked": _bug("withhold", _e("land_run_halt", "unexecuted_withholds"), "17193e",
                           "the evicted C2 pin"),
    "evicted-not-importable": _bug("withhold", _e("land_run_halt", "unexecuted_withholds"), "17193e"),
    "run-dirt-uncommitted": _bug("withhold", _e("halted_record_eviction", "_withhold_run_dirt"),
                                 _q("run-dirt-uncommitted"), denies=False),
    "missing-target": _bug("refusal", _e("landing_targeted_tests", "zero_run_cause_note"), "3c236b-1",
                           "every target path absent from disk, so no test ran"),
    # -- Parks.
    "land-deadline": _bug("park", _e("land_deadline", "halt_reply"), "d0fa7d",
                          "parks a land on a non-verifying step"),
    "dependents_unstarted": _bug("park", _e("ask_contract", "dependents_unstarted_refire_next_action"),
                                 _q("dependents-unstarted"),
                                 "parks the run on held dependents; a refire reproduces the hold (44a104)"),
    "no-folded-stage-manifest": _bug("refusal", _e("ask_compose", "compose_ask_script"),
                                     _q("no-folded-stage-manifest"),
                                     "refuses when the closing agent finds no folded stage manifest (44a104)"),
}

_PROVENANCE_EMITTERS = {
    "py-unparseable": _e("landing_integrity", "unparseable_py"),
    "since-base-overlap": _e("landing_integrity", "since_base_findings"),
    "import-closure-untracked": _e("landing_integrity", "import_closure_findings"),
    "peer-hunks-co-declared": _e("peer_hunk_gate", "co_declared_peer_holds"),
    "out-of-ask-hunks": _e("peer_hunk_gate", "out_of_ask_holds"),
    "unreadable-peer-claims": _e("peer_hunk_gate", "landing_holds"),
    "shipped-claim-unbacked": _e("shipped_claims", "shipped_refusal"),
    "targeted-tests-failed": _e("landing_targeted_tests", "tests_failed_finding"),
    "review-reverify-failed": _e("landing_targeted_tests", "reverify_failed_finding"),
    "review-reverify-indeterminate": _e("landing_targeted_tests", "reverify_indeterminate_finding"),
    "cohold-declared-path": _e("cohold_refusal", "refusal"),
    "xs-over-budget": _e("terminal_commit_refusals", "_xs_over_budget_refusal"),
    "run-touched-left": _TCI,
    "review-edit-unlanded": _TCI,
}

#: Provenance kinds with no refusal or withhold entry of their own; each is recorded, never denies.
PROVENANCE_ONLY = frozenset(PROVENANCE_KINDS - set(_CLASSES))
for _reason in sorted(PROVENANCE_ONLY):
    _CLASSES[_reason] = RefusalClass(
        "withhold", None, False, "since-base-overlap" if _reason == "since-base-overlap" else None,
        _PROVENANCE_EMITTERS[_reason], False, "recorded as provenance; never denies a land",
    )

#: The frozen registry: reason key -> ``RefusalClass``.
LAND_REFUSAL_REGISTRY: Mapping[str, RefusalClass] = MappingProxyType(_CLASSES)

#: The hand-land classes seen on 2026-11-18 and the registry key each reports as.
HAND_LAND_CLASSES = MappingProxyType({
    "missing-target-on-disk": "missing-target",
    "zero-test-verify": "tests-not-run",
    "peer-claim-indeterminate": "peer-claim-indeterminate",
    "land-deadline-park": "land-deadline",
    "evicted-reviewed-pin": "evicted-parked",
    "since-base-overlap": "since-base-overlap",
    "verdict-pin-stale": "verdict-pin-stale",
    "dependents-unstarted-park": "dependents_unstarted",
    "no-folded-stage-manifest": "no-folded-stage-manifest",
})


def classify(reason: str) -> Optional[RefusalClass]:
    """The registry entry for ``reason``, or None when the reason is not registered."""
    return LAND_REFUSAL_REGISTRY.get(reason)


def engine_bugs() -> Mapping[str, RefusalClass]:
    """Every entry marked as denying recoverable work."""
    return {k: v for k, v in LAND_REFUSAL_REGISTRY.items() if v.engine_bug}

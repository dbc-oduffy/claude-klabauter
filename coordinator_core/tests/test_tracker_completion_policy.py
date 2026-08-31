"""
coordinator_core.tests.test_tracker_completion_policy — C1: AC1, AC2, AC3,
AC9, AC12, AC13, AC15; C2: AC4, AC14; C4: AC8, AC9.

Purpose: exercises `coordinator_core.tracker_completion_policy`'s pure
tier classifiers as landed in C1 — the tri-state `code_complete` gate, the
pinned `qa_verified` no-auto-path contract, and the two `render_status`
integration cases (AC12/AC13) that need real `tracker_projection`
behaviour even though the policy module itself stays pure — plus C2's
pure symmetric-retract payload builder and C4's one impure emit seam
(`emit_code_complete_assert`), covering the `code_complete` ASSERT path
only.

Spec backlink: docs/plans/2026-08-18-sat-04-completion-axis-policy.md
§ Acceptance Criteria AC1, AC2, AC3, AC4, AC8, AC9, AC12, AC13, AC14,
AC15; § Tasks C1, C2, C4.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core import tracker_completion_policy as tcp
from coordinator_core import tracker_projection
from coordinator_core import tracker_store
from coordinator_core import tracker_transitions as tt

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _make_git_repo(root):
    """Init a minimal git repository under *root* — mirrors
    `test_tracker_transitions.py`'s `_make_git_repo` (`append_event`'s
    `locked_rmw` resolves its lock directory via `git rev-parse
    --git-common-dir`, so a bare non-git `tmp_path` fails there first).
    """
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args):
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "tracker-completion-policy-test@claude-klabauter.test")
    _git("config", "user.name", "Tracker Completion Policy Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "this-machine")
    return _make_git_repo(tmp_path / "repo")


# ---------------------------------------------------------------------------
# AC1 — classify_code_complete_tier's auto/suggest predicate.
# ---------------------------------------------------------------------------


def test_ac1_all_conditions_true_yields_auto():
    evidence = tcp.CodeCompleteEvidence(
        sha="a" * 40,
        trailer_bound=True,
        reachable_on_default_branch=True,
    )
    assert (
        tcp.classify_code_complete_tier(evidence, closure_fidelity="auto-observable")
        == "auto"
    )


def test_ac1_trailer_not_bound_with_reachable_true_yields_suggest():
    evidence = tcp.CodeCompleteEvidence(
        sha="a" * 40,
        trailer_bound=False,
        reachable_on_default_branch=True,
    )
    assert (
        tcp.classify_code_complete_tier(evidence, closure_fidelity="auto-observable")
        == "suggest"
    )


def test_ac1_trailer_bound_with_reachable_false_yields_suggest():
    evidence = tcp.CodeCompleteEvidence(
        sha="a" * 40,
        trailer_bound=True,
        reachable_on_default_branch=False,
    )
    assert (
        tcp.classify_code_complete_tier(evidence, closure_fidelity="auto-observable")
        == "suggest"
    )


# ---------------------------------------------------------------------------
# AC2 — the tri-state regression guard: None never yields auto, never raises.
# ---------------------------------------------------------------------------


def test_ac2_reachable_none_yields_suggest_and_never_raises():
    evidence = tcp.CodeCompleteEvidence(
        sha="a" * 40,
        trailer_bound=True,
        reachable_on_default_branch=None,
    )
    # No exception is the assertion; the classification is checked too.
    assert (
        tcp.classify_code_complete_tier(evidence, closure_fidelity="auto-observable")
        == "suggest"
    )


def test_ac2_reachable_none_with_trailer_unbound_yields_suggest():
    evidence = tcp.CodeCompleteEvidence(
        sha="a" * 40,
        trailer_bound=False,
        reachable_on_default_branch=None,
    )
    assert (
        tcp.classify_code_complete_tier(evidence, closure_fidelity="auto-observable")
        == "suggest"
    )


# ---------------------------------------------------------------------------
# AC3 — classify_qa_verified is a pinned no-auto-path contract.
# ---------------------------------------------------------------------------


def test_ac3_qa_verified_maximally_strong_evidence_still_suggest():
    evidence = tcp.QaVerifiedEvidence(source="ci-run-authoritative", confidence=1.0)
    assert tcp.classify_qa_verified(evidence) == "suggest"


def test_ac3_qa_verified_minimal_evidence_still_suggest():
    evidence = tcp.QaVerifiedEvidence(source="unknown")
    assert tcp.classify_qa_verified(evidence) == "suggest"


# ---------------------------------------------------------------------------
# AC9 — no subprocess/git/tracker_store-write import anywhere in this
# plan's scope (grep-checkable). Re-checked as of C4: the module now also
# imports `tracker_transitions` (for the emit seam) and `pathlib.Path` (for
# the `repo_root` type) — neither trips this guard, which stays scoped to
# subprocess/git/tracker_store specifically, not "any new import."
# ---------------------------------------------------------------------------


def test_ac9_policy_module_imports_no_subprocess_or_git():
    source = Path(tcp.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*import\s+subprocess\b", source, re.MULTILINE)
    assert not re.search(r"^\s*from\s+subprocess\b", source, re.MULTILINE)
    assert not re.search(r"^\s*import\s+git\b", source, re.MULTILINE)
    assert not re.search(r"^\s*(import|from)\s+.*tracker_store\b", source, re.MULTILINE)


# ---------------------------------------------------------------------------
# C2 — anti-drift assertion: the policy module's `_SUGGEST_TIER` is the
# SAME object as `tracker_transitions`' SSOT, not a redeclared duplicate.
# Guards against the two-tier-vocabulary drift this chunk collapses.
# ---------------------------------------------------------------------------


def test_c2_suggest_tier_is_c1_ssot_not_a_redeclaration():
    assert tcp._SUGGEST_TIER is tt._SUGGEST_TIER


def test_c2_classifiers_return_unchanged_values():
    evidence = tcp.CodeCompleteEvidence(
        sha="a" * 40, trailer_bound=True, reachable_on_default_branch=True
    )
    assert (
        tcp.classify_code_complete_tier(evidence, closure_fidelity="auto-observable")
        == "auto"
    )

    evidence_suggest = tcp.CodeCompleteEvidence(
        sha="b" * 40, trailer_bound=False, reachable_on_default_branch=True
    )
    assert (
        tcp.classify_code_complete_tier(
            evidence_suggest, closure_fidelity="auto-observable"
        )
        == "suggest"
    )

    qa_evidence = tcp.QaVerifiedEvidence(source="ci", confidence=1.0)
    assert tcp.classify_qa_verified(qa_evidence) == "suggest"


# ---------------------------------------------------------------------------
# AC12 — a qa_verified suggestion with NO prior code_complete observation:
# render_status reads "open" (code_complete's None state fails the closed
# conjunct regardless of qa_verified). Asserted explicitly.
# ---------------------------------------------------------------------------


def test_ac12_qa_verified_alone_with_no_code_complete_reads_open(repo_root):
    evidence = tcp.QaVerifiedEvidence(source="ci-run")
    tier = tcp.classify_qa_verified(evidence)
    assert tier == "suggest"

    # A suggest-tier event has applied_at=None and is therefore invisible
    # to read_events/render_status by construction (tracker_transitions'
    # own docstring) — emit it anyway to prove the point explicitly rather
    # than relying on it never having been emitted.
    tt.emit_transition(
        "item-ac12",
        "qa_verified",
        "verified",
        actor="ci",
        evidence={"source": evidence.source},
        tier=tier,
        repo_root=repo_root,
    )

    assert tracker_projection.current_state("item-ac12", "code_complete", repo_root=repo_root) is None
    assert tracker_projection.render_status("item-ac12", repo_root=repo_root) == "open"


# ---------------------------------------------------------------------------
# AC13 — qa_verified regressing independently while code_complete stays
# asserted: render_status reflects both axes independently and does not
# read as closed.
# ---------------------------------------------------------------------------


def test_ac13_qa_verified_regression_with_code_complete_asserted_not_closed(repo_root):
    tt.emit_transition(
        "item-ac13",
        "code_complete",
        "asserted",
        actor="ci",
        evidence={"sha": "b" * 40},
        tier="direct",
        repo_root=repo_root,
    )
    tt.emit_transition(
        "item-ac13",
        "qa_verified",
        "verified",
        actor="ci",
        evidence={"source": "ci-run"},
        tier="direct",
        repo_root=repo_root,
    )
    assert tracker_projection.render_status("item-ac13", repo_root=repo_root) == "closed"

    # Now regress qa_verified independently.
    tt.emit_transition(
        "item-ac13",
        "qa_verified",
        "regressed",
        actor="ci",
        evidence={"source": "ci-rerun-failed"},
        tier="direct",
        repo_root=repo_root,
    )

    assert (
        tracker_projection.current_state("item-ac13", "code_complete", repo_root=repo_root)
        == "asserted"
    )
    assert (
        tracker_projection.current_state("item-ac13", "qa_verified", repo_root=repo_root)
        == "regressed"
    )
    assert tracker_projection.render_status("item-ac13", repo_root=repo_root) == "open"


# ---------------------------------------------------------------------------
# AC4 — detect_symmetric_retract's payload carries the REVERT's own sha,
# never the completing sha, plus its own source_observation_id; returns
# None for a not-trailer-bound / not-reachable-True revert.
# ---------------------------------------------------------------------------


def test_ac4_retract_payload_carries_reverts_sha_never_completing_sha():
    revert_evidence = tcp.CodeCompleteEvidence(
        sha="r" * 40,
        trailer_bound=True,
        reachable_on_default_branch=True,
    )
    payload = tcp.detect_symmetric_retract(
        "item-ac4",
        revert_evidence,
        actor="ci",
        source_observation_id="obs-revert-ac4",
        closure_fidelity="auto-observable",
    )

    assert payload is not None
    assert payload["evidence"] == {"sha": "r" * 40}
    assert payload["evidence"]["sha"] != "c" * 40  # never the completing sha
    assert payload["source_observation_id"] == "obs-revert-ac4"
    assert payload["axis"] == "code_complete"
    assert payload["to_state"] == "retracted"
    assert payload["from_state"] == "asserted"
    assert payload["tier"] == "auto"


@pytest.mark.parametrize(
    "trailer_bound, reachable_on_default_branch",
    [
        (False, True),
        (True, False),
        (True, None),
        (False, None),
    ],
)
def test_ac4_unverified_revert_returns_none(trailer_bound, reachable_on_default_branch):
    revert_evidence = tcp.CodeCompleteEvidence(
        sha="r" * 40,
        trailer_bound=trailer_bound,
        reachable_on_default_branch=reachable_on_default_branch,
    )
    assert (
        tcp.detect_symmetric_retract(
            "item-ac4-unverified",
            revert_evidence,
            actor="ci",
            source_observation_id="obs-revert-unverified",
            closure_fidelity="auto-observable",
        )
        is None
    )


# ---------------------------------------------------------------------------
# AC14 — concurrent revert-detection race: two independent calls for the
# SAME revert observation must compute the SAME dedup address, so a second
# `_emit` of the later payload resolves to the already-stored event from
# the first and exactly one retract event is ever stored. Proved through
# the real addressing/storage path, not by payload equality (equal-by-
# construction of a pure function would test nothing).
# ---------------------------------------------------------------------------


def test_ac14_concurrent_revert_detection_dedupes_to_one_stored_event(repo_root, monkeypatch):
    revert_evidence = tcp.CodeCompleteEvidence(
        sha="r" * 40,
        trailer_bound=True,
        reachable_on_default_branch=True,
    )

    # First establish the code_complete assert this retract logically
    # reverts, so the scenario is realistic (not required by the pure
    # builder itself, but matches the shape a real caller would produce).
    tt.emit_transition(
        "item-ac14",
        "code_complete",
        "asserted",
        actor="ci",
        evidence={"sha": "c" * 40},
        tier="auto",
        source_observation_id="obs-complete-ac14",
        repo_root=repo_root,
    )

    # Two independent (racing) callers detect the SAME revert observation.
    payload1 = tcp.detect_symmetric_retract(
        "item-ac14",
        revert_evidence,
        actor="ci",
        source_observation_id="obs-revert-ac14",
        closure_fidelity="auto-observable",
    )
    payload2 = tcp.detect_symmetric_retract(
        "item-ac14",
        revert_evidence,
        actor="ci",
        source_observation_id="obs-revert-ac14",
        closure_fidelity="auto-observable",
    )
    assert payload1 is not None
    assert payload2 is not None

    # Both racers read the store at the SAME moment (no retract landed for
    # this item yet), so `_code_complete_retract_generation` stamps the
    # SAME generation (0) for each — the actual race condition AC14 guards,
    # not an artifact of calling the two sequentially in this test.
    pre_race_events = list(tracker_store.read_events(repo_root=repo_root))
    generation_at_race = tt._code_complete_retract_generation(
        "item-ac14", pre_race_events
    )
    stamped1 = {**payload1, "generation": generation_at_race}
    stamped2 = {**payload2, "generation": generation_at_race}

    # Go through the REAL addressing path (not payload equality): both
    # payloads must resolve to the same dedup-check AND mint address once
    # the raced generation is stamped, exactly as `_emit` would stamp it.
    dedup_address1 = tt._dedup_check_address(stamped1)
    dedup_address2 = tt._dedup_check_address(stamped2)
    assert dedup_address1 == dedup_address2
    assert dedup_address1 is not None
    assert tt._mint_address(stamped1) == tt._mint_address(stamped2)

    # Racer 1 wins and appends first (through the real `_emit` storage
    # path, which independently stamps the same generation since nothing
    # else has retracted this item yet).
    stored1 = tt._emit(payload1, repo_root=repo_root)

    # Racer 2's `_dedup_check_address` computed under the SAME pre-race
    # snapshot must resolve, against the store as it stands AFTER racer 1's
    # append, to racer 1's already-stored event — this is what `_emit`'s
    # OWN pre-append lookup does internally, over a fresh read: proves a
    # non-racing (sequential) second detection is caught by the ordinary
    # dedup path and never double-appends.
    post_race_events = list(tracker_store.read_events(repo_root=repo_root))
    resolved_for_racer2 = tt._find_existing_by_address(
        dedup_address2, post_race_events
    )
    assert resolved_for_racer2 is not None
    assert resolved_for_racer2["id"] == stored1["id"]

    # Now prove the GENUINELY concurrent case end-to-end through real
    # `_emit` calls: racer 2's read happens BEFORE racer 1's append lands
    # (both racers observe the identical pre-race snapshot), so racer 2's
    # own pre-append dedup lookup misses too. `_mint_transition_event_id`
    # then mints racer 2 the SAME id as racer 1 (same address), so the slow
    # writer collides on `tracker_store.append_event`'s own duplicate-id
    # guard rather than silently double-appending (DR-241 bound (i),
    # `_find_existing_by_address`'s documented closing mitigation).
    real_read_events = tracker_store.read_events
    monkeypatch.setattr(
        tracker_store,
        "read_events",
        lambda *a, **kw: list(pre_race_events),
    )
    try:
        with pytest.raises(tracker_store.TrackerStoreDuplicateIdError):
            tt._emit(payload2, repo_root=repo_root)
    finally:
        monkeypatch.setattr(tracker_store, "read_events", real_read_events)

    events = list(tracker_store.read_events(repo_root=repo_root))
    retracts = [
        event
        for event in events
        if event.get("item_id") == "item-ac14"
        and event.get("axis") == "code_complete"
        and event.get("to_state") == "retracted"
    ]
    assert len(retracts) == 1
    assert tracker_projection.current_state(
        "item-ac14", "code_complete", repo_root=repo_root
    ) == "retracted"


# ---------------------------------------------------------------------------
# AC8 — applied_at regression guard. The mechanism lives in
# `tracker_transitions._emit` (`applied_at = None if payload.get("tier") ==
# _SUGGEST_TIER else observed_at`), shipped by sat-03 and NOT reimplemented
# by `emit_code_complete_assert` (C4) or anywhere in this module. These
# tests exist to guard that sat-03 mechanism from a future edit — so nobody
# reads them and concludes `applied_at` needs to be computed here too.
# ---------------------------------------------------------------------------


def test_ac8_auto_tier_applied_at_set_at_creation(repo_root):
    evidence = tcp.CodeCompleteEvidence(
        sha="a" * 40,
        trailer_bound=True,
        reachable_on_default_branch=True,
    )
    event = tcp.emit_code_complete_assert(
        "item-ac8-auto",
        evidence,
        actor="ci",
        source_observation_id="obs-ac8-auto",
        repo_root=repo_root,
        closure_fidelity="auto-observable",
    )
    assert event["tier"] == "auto"
    assert event["applied_at"] is not None
    assert event["applied_at"] == event["observed_at"]


def test_ac8_direct_tier_applied_at_set_at_creation(repo_root):
    # emit_code_complete_assert always classifies via classify_code_complete_
    # tier, which never returns "direct" — so a "direct" event is emitted
    # straight through tracker_transitions.emit_transition, exactly as a
    # human-triggered caller (outside this plan's scope) would. This test
    # exercises the SAME sat-03 mechanism `emit_code_complete_assert` relies
    # on, not the C4 function itself, per AC8's own wording ("emit at
    # tier=... -> applied_at ...").
    event = tt.emit_transition(
        "item-ac8-direct",
        "code_complete",
        "asserted",
        actor="human",
        evidence={"sha": "b" * 40},
        tier="direct",
        repo_root=repo_root,
    )
    assert event["tier"] == "direct"
    assert event["applied_at"] is not None
    assert event["applied_at"] == event["observed_at"]


def test_ac8_suggest_tier_applied_at_is_none(repo_root):
    evidence = tcp.CodeCompleteEvidence(
        sha="c" * 40,
        trailer_bound=False,
        reachable_on_default_branch=True,
    )
    event = tcp.emit_code_complete_assert(
        "item-ac8-suggest",
        evidence,
        actor="ci",
        source_observation_id="obs-ac8-suggest",
        repo_root=repo_root,
        closure_fidelity="auto-observable",
    )
    assert event["tier"] == "suggest"
    assert event["applied_at"] is None


# ---------------------------------------------------------------------------
# C4 emit-seam behaviour — classification, payload shape, and the scope
# boundary that C4 must NOT cross (never emits C2's retract payload).
# ---------------------------------------------------------------------------


def test_c4_emit_code_complete_assert_classifies_via_c1(repo_root):
    evidence = tcp.CodeCompleteEvidence(
        sha="d" * 40,
        trailer_bound=True,
        reachable_on_default_branch=True,
    )
    event = tcp.emit_code_complete_assert(
        "item-c4-classify",
        evidence,
        actor="ci",
        source_observation_id="obs-c4-classify",
        repo_root=repo_root,
        closure_fidelity="auto-observable",
    )
    assert event["tier"] == tcp.classify_code_complete_tier(
        evidence, closure_fidelity="auto-observable"
    )
    assert event["axis"] == "code_complete"
    assert event["to_state"] == "asserted"
    assert event["evidence"] == {"sha": "d" * 40}
    assert event["source_observation_id"] == "obs-c4-classify"
    assert (
        tracker_projection.current_state(
            "item-c4-classify", "code_complete", repo_root=repo_root
        )
        == "asserted"
    )


def test_c4_emit_code_complete_assert_does_not_accept_a_tier_argument():
    # A caller cannot bypass classify_code_complete_tier by passing a tier
    # directly — the function's signature has no such parameter.
    import inspect

    signature = inspect.signature(tcp.emit_code_complete_assert)
    assert "tier" not in signature.parameters


# ---------------------------------------------------------------------------
# C6 — closure_fidelity is a REQUIRED keyword-only parameter, threaded into
# the shared classifier, and a `verify-with-effort` item can NEVER yield
# `"auto"` no matter how strong its evidence is (adversarial: exhaustively
# re-drives every AC1/AC2 evidence combination that yields "auto" for an
# auto-observable item and asserts each yields "suggest" for a
# verify-with-effort one).
# ---------------------------------------------------------------------------


def test_c6_closure_fidelity_is_required_keyword_only_on_all_three_callers():
    import inspect

    for fn in (
        tcp.classify_code_complete_tier,
        tcp.detect_symmetric_retract,
        tcp.emit_code_complete_assert,
    ):
        signature = inspect.signature(fn)
        param = signature.parameters["closure_fidelity"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "trailer_bound, reachable_on_default_branch",
    [
        (True, True),  # the ONLY combination that yields "auto" for auto-observable
        (False, True),
        (True, False),
        (True, None),
        (False, False),
        (False, None),
    ],
)
def test_c6_verify_with_effort_never_yields_auto(
    trailer_bound, reachable_on_default_branch
):
    evidence = tcp.CodeCompleteEvidence(
        sha="e" * 40,
        trailer_bound=trailer_bound,
        reachable_on_default_branch=reachable_on_default_branch,
    )
    # Adversarial control: confirm this exact evidence combination would
    # yield "auto" for an auto-observable item when (and only when) both
    # conditions hold — proving the verify-with-effort case below is a real
    # degradation, not a vacuous check over inputs that were "suggest"
    # already.
    auto_observable_tier = tcp.classify_code_complete_tier(
        evidence, closure_fidelity="auto-observable"
    )
    if trailer_bound and reachable_on_default_branch is True:
        assert auto_observable_tier == "auto"
    else:
        assert auto_observable_tier == "suggest"

    assert (
        tcp.classify_code_complete_tier(evidence, closure_fidelity="verify-with-effort")
        == "suggest"
    )


def test_c6_closure_fidelity_symbol_is_module_level_frozenset():
    assert "_CLOSURE_FIDELITY_DEGRADES_TO_SUGGEST" in dir(tcp)
    assert isinstance(tcp._CLOSURE_FIDELITY_DEGRADES_TO_SUGGEST, frozenset)
    assert "verify-with-effort" in tcp._CLOSURE_FIDELITY_DEGRADES_TO_SUGGEST


def test_c6_emit_code_complete_assert_verify_with_effort_never_auto(repo_root):
    evidence = tcp.CodeCompleteEvidence(
        sha="f" * 40,
        trailer_bound=True,
        reachable_on_default_branch=True,
    )
    event = tcp.emit_code_complete_assert(
        "item-c6-verify-with-effort",
        evidence,
        actor="ci",
        source_observation_id="obs-c6-verify-with-effort",
        repo_root=repo_root,
        closure_fidelity="verify-with-effort",
    )
    assert event["tier"] == "suggest"
    assert event["applied_at"] is None


def test_c6_detect_symmetric_retract_verify_with_effort_is_suppressed():
    # Finding 3 (EM-adjudicated): the shared classifier's degradation gate
    # applies to detect_symmetric_retract too (not just the assert path),
    # so a verify-with-effort revert never retracts even when its evidence
    # clears the same trailer_bound/reachable gate an auto-observable revert
    # would clear. This is the deliberate, tested consequence — not a
    # silent behavior change.
    revert_evidence = tcp.CodeCompleteEvidence(
        sha="r" * 40,
        trailer_bound=True,
        reachable_on_default_branch=True,
    )
    assert (
        tcp.detect_symmetric_retract(
            "item-c6-retract-suppressed",
            revert_evidence,
            actor="ci",
            source_observation_id="obs-c6-retract-suppressed",
            closure_fidelity="verify-with-effort",
        )
        is None
    )

    # Control: the SAME evidence, auto-observable, DOES retract — proving
    # the suppression above is caused by closure_fidelity, not by the
    # evidence shape.
    payload = tcp.detect_symmetric_retract(
        "item-c6-retract-control",
        revert_evidence,
        actor="ci",
        source_observation_id="obs-c6-retract-control",
        closure_fidelity="auto-observable",
    )
    assert payload is not None
    assert payload["to_state"] == "retracted"

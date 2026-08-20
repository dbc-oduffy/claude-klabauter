"""
coordinator_core.authz.tests.test_authz_contract — authorization contract and drift-guard tests.

Purpose: Verifies the op-class classification (AC8), the fail-closed KeyError semantic
(AC4/F1), and the drift-guard (AC4/F0) that detects any op registered in the live
_REGISTRY without a corresponding OP_CLASSIFICATION entry.

Negative-spec (DR-215/C8 — vacated tests removed):
    TestIsAuthorized (two-tier token scope matrix), TestRequiresSingleWriterQueue, and
    the HTTP-gate / _OP_KEY_SCOPE tests in TestMemoSendClassification are removed with
    the UDS-auth / per-partition-token machinery they covered (DR-215/C8). The surviving
    surface is the op-class classification framework only.

Test convention: pytest (see pyproject.toml [tool.pytest.ini_options] at repo root).
Invoke via: ``pytest coordinator_core/authz/tests/ -v``

Spec backlink: pln-pcore-05-invoke-op-write-seman-80eecd § C1
Decision:      docs/decisions/DR-208-invoke-op-authz-model.md § AC4 / AC8
"""

from __future__ import annotations

import inspect
import re

# IMPORTANT: coordinator_core.ops MUST be imported before coordinator_core.ipc._REGISTRY
# is read. Importing coordinator_core.ops triggers the register_op() side-effects that
# populate _REGISTRY. Importing coordinator_core.ipc alone leaves _REGISTRY == {} and
# makes any registry-size assertion vacuously green (the Staff Engineer F0 — the vacuous-pass hazard).
import coordinator_core.ops  # noqa: F401 — import for register_op side-effects
import coordinator_core.ops.fleet.memo_draft
import coordinator_core.ops.fleet.memo_compose
import coordinator_core.ipc

import pytest

from coordinator_core.authz.classification import (
    OpClass,
    OP_CLASSIFICATION,
    classify,
)


# ---------------------------------------------------------------------------
# classify() fail-closed tests (AC4/F1)
# ---------------------------------------------------------------------------

class TestClassify:
    # Review: code-reviewer — parametrize so each op gets its own pass/fail signal; a broken
    # "ping" classification no longer masks a simultaneously broken "cutover.gate".
    @pytest.mark.parametrize("op_name", ["ping", "cutover.gate", "handoff.has_live_children"])
    def test_known_ops_return_correct_class(self, op_name: str) -> None:
        assert classify(op_name) is OpClass.COMPUTE_ONLY

    def test_ceremony_chunk_commits_is_compute_only(self) -> None:
        """2026-08-10 fix (state/bug-backlog/2026-08-10-chain-ancestry-waivers-reap-and-
        ceremony-e5afd3e0e7ab.yaml): was registered+module-mapped but missing an
        OP_CLASSIFICATION entry entirely (raised KeyError, outside the frozen
        _KNOWN_UNCLASSIFIED_OPS_DEBT baseline). Pure git-log read — no write anywhere in
        coordinator_core/ops/ceremony/chunk_commits.py."""
        assert classify("ceremony.chunk_commits") is OpClass.COMPUTE_ONLY

    def test_unknown_op_raises_key_error(self) -> None:
        """classify() raises KeyError on an unclassified op — fail-closed.

        At dispatch the caller MUST treat this KeyError as DENY, never as COMPUTE_ONLY
        and never swallowed into a default-allow.
        See DR-208 § Fail-closed runtime semantic.
        """
        with pytest.raises(KeyError):
            classify("nonexistent.op")

    def test_empty_string_op_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            classify("")

    def test_partial_match_raises_key_error(self) -> None:
        # "ping" is known but "pin" is not — no prefix matching
        with pytest.raises(KeyError):
            classify("pin")


# ---------------------------------------------------------------------------
# Drift-guard (AC4 / the Staff Engineer F0) — detect-then-fail-loud gate
#
# This test is the CI half of the fail-closed doctrine. It ensures that every op
# registered in the live _REGISTRY has a classification in OP_CLASSIFICATION.
# A future op added to coordinator_core.ops without a classification entry will
# cause this test to fail loud rather than silently pass as COMPUTE_ONLY.
#
# Import order is load-bearing: coordinator_core.ops must be imported before reading
# coordinator_core.ipc._REGISTRY (done at module level above — see the IMPORTANT note).
# ---------------------------------------------------------------------------

class TestDriftGuard:
    """Detect-then-fail-loud gate: every registered op must have a classification."""

    def test_registry_is_non_empty(self) -> None:
        """Positive floor — a vacuously-empty registry must not pass (the Staff Engineer F0).

        If coordinator_core.ops was NOT imported, _REGISTRY is {} and the coverage
        assertion below would pass over zero ops (vacuously green). This assertion
        catches that hazard by requiring at least 3 ops (the pcore-03 beachhead set).
        """
        assert len(coordinator_core.ipc._REGISTRY) >= 3, (
            f"Expected at least 3 ops in _REGISTRY; got {len(coordinator_core.ipc._REGISTRY)}. "
            "This likely means coordinator_core.ops was not imported before this test ran, "
            "leaving _REGISTRY empty and making drift assertions vacuously green."
        )

    # The strict-xfail marker that stood here recorded 65 ops registered without an
    # OP_CLASSIFICATION entry (debt-backlog
    # state/debt-backlog/2026-07-23-authz-drift-guard-ops-registered-without-52137f1ff6b9.yaml,
    # PM-ratified under DR-208 § "Fail-closed runtime semantic"). Its own exit
    # condition was "draining the 65 XPASSes this test and forces removal of the
    # marker" — C17 of docs/plans/2026-08-20-a-refusal-cannot-exit-zero.md drained the
    # last of them, so the marker is removed rather than left to XPASS-fail. The
    # predicate below was never rewritten and is unchanged; it is now simply green.
    def test_all_registered_ops_are_classified(self) -> None:
        """Every op name in the live _REGISTRY has an entry in OP_CLASSIFICATION.

        Fails loud if a future op is added to coordinator_core.ops without adding a
        classification to OP_CLASSIFICATION. This is the CI half of the fail-closed
        doctrine; the runtime half is classify() raising KeyError => dispatch DENY.
        See DR-208 § Fail-closed runtime semantic and § Classification correctness discipline.
        """
        unclassified = [
            name
            for name in coordinator_core.ipc._REGISTRY
            if name not in OP_CLASSIFICATION
        ]
        assert unclassified == [], (
            f"Ops registered in _REGISTRY but missing from OP_CLASSIFICATION: {unclassified}. "
            "Add an OP_CLASSIFICATION entry in coordinator_core/authz/classification.py. "
            "New ops default to MUTATING until a reviewer affirms COMPUTE_ONLY — "
            "see DR-208 § Classification correctness discipline."
        )

    def test_no_stale_classification_entries(self) -> None:
        """Every name in OP_CLASSIFICATION has a corresponding op in the live _REGISTRY.

        Review: code-reviewer — converse of test_all_registered_ops_are_classified. Guards
        the other direction: an op removed from coordinator_core.ops without removing its
        OP_CLASSIFICATION entry would leave dead config that classify() still serves, creating
        an authz surface for an op that cannot actually be dispatched. Both directions must pass.
        """
        stale = [
            name
            for name in OP_CLASSIFICATION
            if name not in coordinator_core.ipc._REGISTRY
        ]
        assert stale == [], (
            f"Ops in OP_CLASSIFICATION but absent from _REGISTRY: {stale}. "
            "Remove the stale entry from OP_CLASSIFICATION in "
            "coordinator_core/authz/classification.py. "
            "A stale entry creates an authz surface (classify() returns a class) for an op "
            "that cannot actually be dispatched."
        )

    def test_known_beachhead_ops_are_present(self) -> None:
        """Confirm the three pcore-03 beachhead ops are individually registered."""
        for op_name in ("ping", "cutover.gate", "handoff.has_live_children"):
            assert op_name in coordinator_core.ipc._REGISTRY, (
                f"Expected beachhead op {op_name!r} to be in _REGISTRY but it was absent. "
                "If the op was renamed, update OP_CLASSIFICATION and this assertion."
            )


# ---------------------------------------------------------------------------
# Three-way registration-count reconciliation (C3,
# docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C3)
#
# The plan chunk that authored this section asked whether the live _REGISTRY /
# OP_MODULE_MAP / _OP_KEY_SCOPE counts disagree for a legitimate reason or because
# a registration surface has an undetected hole. Re-derived at C3 integration time
# (not trusted from any earlier prose figure, which drifts as the tree moves):
#
#   live _REGISTRY size                          254
#   OP_MODULE_MAP size (ops/_registry_map.py)     252 -> 254 after this chunk
#   _OP_KEY_SCOPE size (op_scopes.py)             254
#   OP_CLASSIFICATION size (authz/classification.py) 245
#
# Disposition of each gap:
#
#  * OP_MODULE_MAP was short two entries — "peer_notice.send" / "peer_notice.check"
#    were registered (present in _REGISTRY, _OP_KEY_SCOPE, OP_CLASSIFICATION) but
#    never added to OP_MODULE_MAP. This IS the class of gap coordinator_core.authz.
#    registration_quad.check_registration_quad() exists to catch, and it does catch
#    it (both op_keys surface as OP_MODULE_MAP-missing QuadViolations, unfiltered by
#    either known-debt ledger) — it was simply outstanding, not undetected. Fixed in
#    this chunk (coordinator_core/ops/_registry_map.py); the regression test below
#    pins it shut. Per _registry_map.py's own docstring this gap degraded silently
#    to the eager-import fallback rather than breaking dispatch, which is why the
#    live system stayed correct while the map itself lagged.
#
#  * The live-_REGISTRY-vs-OP_CLASSIFICATION gap (254 vs 245, 9 unclassified at C3
#    time) is the SAME gap test_all_registered_ops_are_classified below already
#    covers via a strict xfail against coordinator_core.authz.registration_quad's
#    frozen `_KNOWN_UNCLASSIFIED_OPS_DEBT` baseline (65 entries recorded
#    2026-07-25; most have since been individually classified without the debt
#    entry being pruned, which is a known-shrinking-not-growing direction the
#    baseline's own never-grows guard in test_registration_quad.py enforces — that
#    guard, and pruning the now-stale entries, is registration_quad.py's file, not
#    this chunk's writable scope). test_all_registered_ops_are_classified itself
#    is NOT a hole: it is a strict xfail with a named owning debt-backlog entry
#    (state/debt-backlog/2026-07-23-authz-drift-guard-ops-registered-without-
#    52137f1ff6b9.yaml) and it xfails (not xpasses) at C3 HEAD — see
#    `test_registry_is_non_empty`'s sibling assertions above for the vacuous-pass
#    guard that would catch a silently-empty registry masking this.
#
#  * The remaining _REGISTRY vs OP_MODULE_MAP/_OP_KEY_SCOPE parity (254 == 254 for
#    _OP_KEY_SCOPE; 254 == 254 for OP_MODULE_MAP once the peer_notice.* fix above
#    lands) has no unexplained residue: every registered op now has one of "common_
#    dir" / "show_top" / "none" in _OP_KEY_SCOPE and a lazy-import module path in
#    OP_MODULE_MAP, which is what C12's MUTATING/COMPUTE_ONLY partition (downstream
#    of OP_CLASSIFICATION, not this section) depends on being trustworthy for.
#
# Three residual QuadViolations found by check_registration_quad() at C3 time
# (app_session.launch / app_session.census / app_session.teardown missing
# OP_CLASSIFICATION) live in coordinator_core/authz/classification.py, outside
# this chunk's writable file list — reported to the dispatching EM as a residual,
# not fixed here.
# ---------------------------------------------------------------------------

class TestOpModuleMapRegistrationCoverage:
    """OP_MODULE_MAP (the lazy-import performance seam) covers every op the live
    _REGISTRY knows about — pins the peer_notice.* gap C3 found and fixed shut."""

    def test_peer_notice_ops_are_in_module_map(self) -> None:
        from coordinator_core.ops._registry_map import OP_MODULE_MAP

        for op_name in ("peer_notice.send", "peer_notice.check"):
            assert op_name in OP_MODULE_MAP, (
                f"{op_name!r} is registered but missing from OP_MODULE_MAP "
                "(coordinator_core/ops/_registry_map.py) — reintroduces the C3 "
                "three-way-count reconciliation gap."
            )

    def test_registered_ops_missing_from_module_map(self) -> None:
        """No live-registered op should be absent from OP_MODULE_MAP.

        A missing entry does not break dispatch today (the module falls back to
        importing the whole coordinator_core.ops package on a registry MISS — see
        _registry_map.py's own docstring), but an absence here is exactly the kind
        of silent drift C3 was asked to reconcile, so this pins the map complete
        against whatever is live in _REGISTRY at test time.

        Review: code-reviewer (P3) -- largely redundant with
        coordinator_core.authz.registration_quad.check_registration_quad(), which is
        itself run live and asserted green (against the frozen known-debt allowlists)
        by test_registration_quad.py::TestKnownIncompleteRegistrationsLedger::
        test_live_tree_is_green_after_filtering_known_debt -- that assertion would
        already have failed loud on the peer_notice.* OP_MODULE_MAP gap this test
        pins, since module_map misses are not among the allowlisted surfaces. Kept
        anyway: this test isolates a single surface (OP_MODULE_MAP) with a narrower,
        more directly actionable failure message naming the exact file to edit,
        rather than the quad check's four-surface QuadViolation report -- a real,
        if modest, pin the quad check doesn't provide on its own. Not deleted on a
        guess.
        """
        from coordinator_core.ops._registry_map import OP_MODULE_MAP

        missing = sorted(
            name for name in coordinator_core.ipc._REGISTRY if name not in OP_MODULE_MAP
        )
        assert missing == [], (
            f"Ops registered in _REGISTRY but missing from OP_MODULE_MAP: {missing}. "
            "Add an entry in coordinator_core/ops/_registry_map.py::OP_MODULE_MAP."
        )

    def test_every_mapped_op_is_reachable_on_the_production_import_path(self) -> None:
        """Every OP_MODULE_MAP key must resolve to a handler after the production import.

        The opposite direction of the test above, and the one that catches a DEAD
        capability rather than a bookkeeping gap. `test_registered_ops_missing_from_
        module_map` iterates _REGISTRY, so an op whose module never gets imported is
        invisible to it — it cannot report the very ops whose unreachability is the
        defect. Measured 2026-08-19: `workflow.fire` and `workflow.fire_status` were
        healthy on disk and registered fine on a direct module import, but
        `get_op_handler("workflow.fire")` returned None on every path a real caller
        uses, because `_eager_import_all` walks the hand-maintained _EAGER_OP_MODULES
        list and that list did not name the module. The documented consumer — the
        headless/cron `emit-dispatch-workflow --fire` path — was dead, and the
        safe-fallback import that exists for a STALE map entry does not save a
        MISSING one.

        Bug: state/bug-backlog/2026-08-19-three-authz-guards-red-on-head-ten-ops-r-345344e48218.yaml
        """
        import coordinator_core.ops  # noqa: F401 — the production registration path
        from coordinator_core.ops._registry_map import OP_MODULE_MAP

        unreachable = sorted(
            name for name in OP_MODULE_MAP if name not in coordinator_core.ipc._REGISTRY
        )
        assert unreachable == [], (
            f"Ops in OP_MODULE_MAP that do not register on the production import "
            f"path, so no caller can dispatch them: {unreachable}. Add the owning "
            "module to _EAGER_OP_MODULES in coordinator_core/ops/__init__.py."
        )


# ---------------------------------------------------------------------------
# memo.send classification tests (strang-03 C3)
#
# AC3 (strang-03): memo.send is classified MUTATING.
# The HTTP-gate test (test_memo_send_http_gate_fires) and the _OP_KEY_SCOPE routing-seam
# test (test_memo_send_scope_is_common_dir) are removed with the UDS-auth / per-request
# routing machinery (DR-215/C8 and C5 respectively).
#
# Spec backlink: pln-strang-03-cross-repo-memo-send-40d84e § C3
# ---------------------------------------------------------------------------

class TestMemoSendClassification:
    """memo.send classifies MUTATING."""

    def test_memo_send_classifies_mutating(self) -> None:
        """memo.send is explicitly MUTATING (new op, fail-closed default affirmed).

        DR-208 five-question affirmation: writes one dirty file into a sibling repo's
        cross-repo/inbox/ (Q1 YES), not rag's store (Q2 No), opens file for write (Q3 YES),
        mutates cross-repo substrate shared across EM sessions (Q4 YES), observable across
        process boundaries (Q5 YES). All YES → MUTATING is the correct classification.
        """
        assert classify("memo.send") is OpClass.MUTATING

    def test_memo_send_is_registered(self) -> None:
        """memo.send is registered in the live _REGISTRY (import side-effect of ops/__init__)."""
        assert "memo.send" in coordinator_core.ipc._REGISTRY, (
            "memo.send is not in _REGISTRY — ensure coordinator_core.ops.fleet.memo_send "
            "is imported in coordinator_core/ops/__init__.py."
        )


# ---------------------------------------------------------------------------
# deliverable.rollup classification tests (factsupply-op C3)
#
# AC: deliverable.rollup is classified COMPUTE_ONLY (read-only resolver, zero git subprocess).
# Both the OP_CLASSIFICATION membership and the live _REGISTRY membership are asserted
# explicitly — the drift-guard >= N floor is not weakened; these are additive assertions.
#
# Spec backlink: pln-claude-klabauter-deliverable-spine-fact--cd004e § C3
# ---------------------------------------------------------------------------

class TestDeliverableRollupClassification:
    """deliverable.rollup classifies COMPUTE_ONLY and is registered."""

    def test_deliverable_rollup_in_op_classification(self) -> None:
        """deliverable.rollup has an explicit entry in OP_CLASSIFICATION."""
        assert "deliverable.rollup" in OP_CLASSIFICATION, (
            "deliverable.rollup is missing from OP_CLASSIFICATION — add a COMPUTE_ONLY "
            "entry with DR-208 five-question affirmation in coordinator_core/authz/classification.py."
        )

    def test_deliverable_rollup_classifies_compute_only(self) -> None:
        """deliverable.rollup is explicitly COMPUTE_ONLY (read-only resolver, no git subprocess).

        DR-208 five-question affirmation: zero writes of any kind (Q1 No), no rag store write
        (Q2 No), no file opened for write (Q3 No), no shared mutable state outside module
        (Q4 No), no cross-process side effects (Q5 No). All No → COMPUTE_ONLY.
        Zero git subprocess — stricter than commit.anchors; the commit.anchors read-only git
        carve-out does NOT apply here and does NOT transfer.
        """
        assert classify("deliverable.rollup") is OpClass.COMPUTE_ONLY

    def test_deliverable_rollup_is_registered(self) -> None:
        """deliverable.rollup is registered in the live _REGISTRY (import side-effect of ops/__init__)."""
        assert "deliverable.rollup" in coordinator_core.ipc._REGISTRY, (
            "deliverable.rollup is not in _REGISTRY — ensure coordinator_core.ops.deliverable_rollup "
            "is imported in coordinator_core/ops/__init__.py."
        )


# ---------------------------------------------------------------------------
# memo.draft / memo.compose classification tests (2026-07-21 review, Finding 1
# of the memo-clean-split-op-coverage slice review)
#
# Both ops write a file (memo.draft: O_EXCL create; memo.compose: os.replace
# in-place edit) and were previously self-contradictingly classified
# COMPUTE_ONLY. This section pins the corrected MUTATING classification and
# adds a write-signal drift-guard so a future self-contradicting entry for
# these two ops fails loud rather than silently regressing.
#
# Spec backlink: pln-memo-tool-rebuild-claude-klabauter-owns--bd5745 § C7
# Decision:      docs/decisions/DR-208-invoke-op-authz-model.md § 5 (fail-closed)
# ---------------------------------------------------------------------------

# Modules whose handler is asserted to write disk (Q1/Q3 YES) and therefore must
# never be classified COMPUTE_ONLY. Narrowly scoped to the two ops this finding
# concerns, not a repo-wide AST sweep — see the module-level docstring TODO below
# for the broader-scan follow-up this narrower check does not attempt.
_MEMO_WRITE_OP_MODULES = {
    "memo.draft": coordinator_core.ops.fleet.memo_draft,
    "memo.compose": coordinator_core.ops.fleet.memo_compose,
}

# Grep-level write-signal pattern: os.open with a write/create flag, open(...) in
# a write/append/exclusive-create text mode, or os.replace (atomic write-in-place).
# This is a source-text signal, not a full AST data-flow proof — sufficient to
# catch "this module's own source contains a disk-write call" regressions, which
# is exactly the shape Finding 1 identified (an entry whose own five-question
# affirmation says YES to writing but is classified COMPUTE_ONLY anyway).
_WRITE_SIGNAL_RE = re.compile(
    r"os\.open\([^)]*O_(?:CREAT|WRONLY|EXCL|APPEND|TRUNC)"
    r"|open\([^)]*[\"'][wxa]"
    r"|os\.replace\("
)


class TestMemoDraftComposeClassification:
    """memo.draft/memo.compose classify MUTATING (Finding 1 correction)."""

    @pytest.mark.parametrize("op_name", ["memo.draft", "memo.compose"])
    def test_memo_draft_compose_classify_mutating(self, op_name: str) -> None:
        """Both ops write a file and must classify MUTATING, not COMPUTE_ONLY.

        DR-208 five-question affirmation: Q1 (writes a state file) is YES for
        both — memo.draft creates via os.open(O_CREAT|O_EXCL|O_WRONLY), memo.compose
        edits in place via os.replace. A YES answer to Q1 requires MUTATING per
        DR-208's fail-closed rule; COMPUTE_ONLY was a self-contradicting entry.
        """
        assert classify(op_name) is OpClass.MUTATING

    @pytest.mark.parametrize("op_name", ["memo.draft", "memo.compose"])
    def test_memo_draft_compose_are_registered(self, op_name: str) -> None:
        assert op_name in coordinator_core.ipc._REGISTRY, (
            f"{op_name} is not in _REGISTRY — ensure its handler module is imported "
            "in coordinator_core/ops/__init__.py."
        )

    @pytest.mark.parametrize("op_name", sorted(_MEMO_WRITE_OP_MODULES))
    def test_write_signal_ops_are_not_compute_only(self, op_name: str) -> None:
        """Drift-guard: a handler module whose source contains a disk-write call
        must never be classified COMPUTE_ONLY.

        This is the positive assertion Finding 1 asked for — it cross-checks each
        op's classification against a source-level signal that its handler module
        writes disk (os.open with a write/create flag, open(..., 'w'/'x'/'a'), or
        os.replace), independent of whether the entry's own five-question comment
        happens to (mis)state the answer. Scoped to the two ops this finding
        concerns; TODO: widen to a repo-wide AST/grep sweep over every registered
        op's handler module once a safe module-name -> op-name resolution mapping
        exists for the full registry (today op names and module import paths are
        not 1:1 derivable without a manual mapping table).
        """
        module = _MEMO_WRITE_OP_MODULES[op_name]
        source = inspect.getsource(module)
        assert _WRITE_SIGNAL_RE.search(source), (
            f"{op_name}'s handler module ({module.__name__}) no longer matches the "
            "expected write-signal pattern — if the write was removed, COMPUTE_ONLY "
            "may now be correct and this test/mapping should be updated deliberately "
            "(not silently); if the write is still present, this assertion is broken."
        )
        assert classify(op_name) is OpClass.MUTATING, (
            f"{op_name}'s handler module ({module.__name__}) contains a disk-write "
            "signal but is classified COMPUTE_ONLY — this is the exact self-"
            "contradicting-entry shape Finding 1 identified. Fix the classification "
            "in coordinator_core/authz/classification.py, do not weaken this test."
        )

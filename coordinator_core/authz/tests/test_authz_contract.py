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
    # "ping" classification no longer masks a simultaneously broken "coverage.gate".
    @pytest.mark.parametrize("op_name", ["ping", "coverage.gate", "handoff.has_live_children"])
    def test_known_ops_return_correct_class(self, op_name: str) -> None:
        assert classify(op_name) is OpClass.COMPUTE_ONLY

    def test_ceremony_chunk_commits_is_compute_only(self) -> None:
        """2026-08-10 fix (state/bug-backlog/2026-08-10-chain-ancestry-waivers-reap-and-
        ceremony-e5afd3e0e7ab.yaml): was registered+module-mapped but missing an
        OP_CLASSIFICATION entry entirely (raised KeyError, outside the frozen
        _KNOWN_UNCLASSIFIED_OPS_DEBT baseline). Pure git-log read — no write anywhere in
        coordinator_core/ops/ceremony/chunk_commits.py."""
        assert classify("ceremony.chunk_commits") is OpClass.COMPUTE_ONLY

    def test_chain_ancestry_waivers_reap_is_mutating(self) -> None:
        """2026-08-10 fix, same bug entry as above. Deletes waiver files/subdirectories
        (coordinator_core/ops/reap_chain_ancestry_waivers.py — .unlink()/.rmdir()) — a
        write to coordinator substrate, classified MUTATING even though the module's own
        docstring frames deletion as fail-safe/remove-only (that property bounds the
        DIRECTION of harm, not whether a write happens)."""
        assert classify("chain_ancestry_waivers.reap") is OpClass.MUTATING

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

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Known debt: 65 ops registered but missing an OP_CLASSIFICATION entry, "
            "frozen as coordinator_core.authz.registration_quad._KNOWN_UNCLASSIFIED_OPS_DEBT. "
            "Owning debt-backlog entry: "
            "state/debt-backlog/2026-07-23-authz-drift-guard-ops-registered-without-"
            "52137f1ff6b9.yaml. PM-ratified amendment: DR-208 § 'Fail-closed runtime "
            "semantic' (:134-150). This marker is a strict xfail, not a rewritten "
            "assertion — the predicate below is untouched, so draining the 65 XPASSes "
            "this test and forces removal of the marker."
        ),
    )
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
        for op_name in ("ping", "coverage.gate", "handoff.has_live_children"):
            assert op_name in coordinator_core.ipc._REGISTRY, (
                f"Expected beachhead op {op_name!r} to be in _REGISTRY but it was absent. "
                "If the op was renamed, update OP_CLASSIFICATION and this assertion."
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

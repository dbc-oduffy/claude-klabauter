"""
coordinator_core.bash_guards.tests.test_grant_deny_distinguishes_a_live_record
— the grant leg's deny must tell "no grant" apart from "a grant that did not
authorize this".

Reported by example-retrieval-repo-ue-addon-em, 2026-09-02
(`cross-repo/inbox/2026-09-02-example-retrieval-repo-ue-addon-em-merge-assemble-apply-
unreachable-in-consumer-repos.md`, item 2): a live, valid ceremony grant read
back fine through `tier-u-grant-cli` and through `check_tier_u_grant`
in-process, yet every real Bash-tool invocation denied on the grant leg. The
deny they got was the ordinary no-grant text, whose only offered escape is
`tier-u-grant-cli grant pm "<verbatim PM utterance>"` — so the one route out
of a FALSE-NEGATIVE grant check was to type a PM utterance authorizing
something the PM had not authorized. They declined, correctly.

`check_tier_u_grant` already returns the parsed record on a denial, by its
own documented contract, exactly so a denial can quote it. `check()` captured
it into `_record` and discarded it, so both cases rendered identically.

NEGATIVE SPEC: this file asserts only what the deny TEXT distinguishes and
offers. It does not assert which sid is correct, does not exercise
`check_tier_u_grant`'s own authorization logic (that is `session/`'s, and is
deliberately untouched), and takes no position on which gate the 2026-09-02
incident actually hit. Nothing on this host reproduces it: the live hook
chain here resolves a native `cwd`, the same sid a Bash child does, and reads
a record written under it (verified 2026-09-02 with a deliberately
never-valid probe record). The point of this branch is that the next
occurrence, on whichever box, states its own cause instead of costing another
reader twenty minutes of bisection.

No process spawn, no git — fast tier.
"""

from __future__ import annotations

import coordinator_core.bash_guards.check_test_suite_invocation as ctsi

_RECORD = {
    "session_id": "0d60b2ee-7a60-4f46-a5f2-173e5a50ae63",
    "granted_by": "ceremony",
    "ceremony": "merging-to-main",
}


def _render(**kwargs) -> str:
    return ctsi._deny_reason_grant("pytest", "pytest tests/", **kwargs)


class TestNoRecordBranchIsUnchanged:
    """The ordinary no-grant deny is the correct text for a reader who
    genuinely has no grant, and this change must not have touched it."""

    def test_absent_record_still_asks_the_pm(self) -> None:
        rendered = _render(ungranted_record=None)
        assert "Ask the PM for a Tier-U authorization grant" in rendered
        assert 'tier-u-grant-cli grant pm "<verbatim PM utterance>"' in rendered

    def test_tie_branch_still_names_no_tier_f_escape(self) -> None:
        assert "No Tier-F escape" in _render(is_tie=True, ungranted_record=None)


class TestRecordPresentBranch:
    def test_it_does_not_tell_the_reader_to_type_a_pm_utterance(self) -> None:
        """The defect that made the incident unrecoverable without
        fabricating an authorization."""
        rendered = _render(ungranted_record=_RECORD)
        assert "tier-u-grant-cli grant pm" not in rendered
        assert "Ask the PM for a Tier-U authorization grant" not in rendered

    def test_it_names_both_sids_and_the_granter(self) -> None:
        rendered = _render(ungranted_record=_RECORD)
        assert _RECORD["session_id"] in rendered
        assert "ceremony" in rendered
        assert "this process sid:" in rendered

    def test_it_states_that_asking_again_will_not_help(self) -> None:
        rendered = _render(ungranted_record=_RECORD)
        assert "asking for another will not change that" in rendered

    def test_it_names_the_failing_gate_rather_than_the_possibilities(self) -> None:
        """Superseded a weaker assertion: the first version of this branch
        listed both readings of the sid pair and left the reader to work out
        which they had. Enumerating possibilities is what the reader could
        already do; naming the gate is the thing they could not."""
        rendered = _render(ungranted_record=_RECORD, ungranted_cwd=None)
        assert "why:" in rendered
        assert "writer/reader disagreement" in rendered

    def test_a_record_missing_its_fields_still_renders(self) -> None:
        """A malformed record reaches this branch too — `check_tier_u_grant`
        returns whatever parsed as an object. It must not raise."""
        rendered = _render(ungranted_record={})
        assert "<absent>" in rendered
        assert "grant session_id:" in rendered

    def test_the_grant_detail_pointer_survives(self) -> None:
        """`_deny_reason_grant`'s own docstring carries a NEGATIVE SPEC
        against dropping this pointer to reclaim budget; the new branch is
        held to it too."""
        assert ctsi._GRANT_DETAIL_POINTER in _render(ungranted_record=_RECORD)


class TestDiagnosticSidHelper:
    def test_it_never_raises(self) -> None:
        """It annotates a deny; it may not be able to break one."""
        assert ctsi._resolved_sid_for_diagnosis() is None or isinstance(
            ctsi._resolved_sid_for_diagnosis(), str
        )


class TestFailingGateIsNamed:
    """The deny names WHICH of `check_tier_u_grant`'s gates rejected the
    record, not merely the two readings of the sid pair.

    Without this the reader still has to bisect: "sids match or they don't"
    leaves them running the predicate by hand, which is what cost
    example-retrieval-repo-ue-addon-em ~20 minutes. `_ungranted_record_failing_gate`
    mirrors that function's gate ORDER so the named gate is the one it
    actually returned on.
    """

    def test_unrecognised_granter_is_named(self) -> None:
        why = ctsi._ungranted_record_failing_gate(
            {"granted_by": "DIAGNOSTIC", "session_id": "abc"}, "abc", None
        )
        assert "not a granter this engine recognises" in why

    def test_ceremony_granter_without_a_ceremony_is_named(self) -> None:
        why = ctsi._ungranted_record_failing_gate(
            {"granted_by": "ceremony", "session_id": "abc"}, "abc", None
        )
        assert "names no ceremony" in why

    def test_pm_granter_with_a_ceremony_is_named(self) -> None:
        why = ctsi._ungranted_record_failing_gate(
            {"granted_by": "pm", "ceremony": "merging-to-main", "session_id": "abc"},
            "abc",
            None,
        )
        assert "also names a ceremony" in why

    def test_sid_mismatch_is_named_as_an_engine_defect(self) -> None:
        """The 2026-09-02 shape, if that is what it turns out to be: the
        reader is told it is not theirs to fix and what to report."""
        why = ctsi._ungranted_record_failing_gate(
            {"granted_by": "ceremony", "ceremony": "m", "session_id": "someone-else"},
            "mine",
            None,
        )
        assert "writer/reader disagreement" in why
        assert "engine defect" in why

    def test_gate_order_matches_the_predicate_it_mirrors(self) -> None:
        """A record failing SEVERAL gates reports the FIRST one, matching
        `check_tier_u_grant`'s own short-circuit order -- otherwise the named
        gate is a true statement about the record but not the reason it was
        refused."""
        why = ctsi._ungranted_record_failing_gate(
            {"granted_by": "nonsense", "session_id": "someone-else"}, "mine", None
        )
        assert "not a granter this engine recognises" in why
        assert "writer/reader disagreement" not in why

    def test_it_never_raises_on_a_malformed_record(self) -> None:
        assert isinstance(ctsi._ungranted_record_failing_gate({}, None, None), str)

    def test_the_rendered_deny_carries_the_why_line(self) -> None:
        rendered = _render(
            ungranted_record={"granted_by": "nonsense", "session_id": "abc"},
            ungranted_cwd=None,
        )
        assert "why:" in rendered
        assert "not a granter this engine recognises" in rendered

"""Tests for the grouping-approval authorization predicate (2026-07-29).

Contract: cross-repo/archive/2026-07-29-doe-claude-em-grouping-approval-contract.md
(actioned; moved from inbox/ to archive/), as amended by our reply (DoE-claude
cross-repo/inbox/2026-07-29-claude-klabauter-em-grouping-approval-contract-confirmed.md).

The predicate replaces the per-row `pm_approved` boolean with three
plan-level `grouping_approvals` blocks. Membership derives from each row's
`disposition` and is stored nowhere; each block carries a `sha256:`-prefixed
digest over its OWN membership set only.

These tests carry the contract's negative-spec as executable assertions,
because the negative-spec is where this design's value lives: a digest that
reacted to prose or row order would expire for reasons unrelated to what was
approved, which trains the re-stamp reflex the whole change exists to
prevent.
"""

from __future__ import annotations

import pytest

from coordinator_core.frontmatter.schema_validate import (
    _cf_plan_tasks_disposition_shape,
    check_plan_tasks_grouping_approval,
    check_plan_tasks_source,
    compute_grouping_digest,
    is_governed_plan,
)


def _plan(
    tasks_yaml: str,
    *,
    frontmatter: str | None = None,
    prose: str = '',
) -> str:
    """A minimal plan document, optionally with frontmatter and body prose."""
    head = f"---\n{frontmatter}\n---\n" if frontmatter is not None else ''
    return (
        f"{head}"
        "# A plan\n\n"
        f"{prose}"
        "## Tasks\n\n"
        "```yaml plan-tasks\n"
        f"{tasks_yaml}\n"
        "```\n"
    )


def _rows(tasks_yaml: str) -> list[dict]:
    import yaml
    return yaml.safe_load(tasks_yaml)


# A defer grouping holding exactly one backlogged row, and its correct
# digest.
#
# Review: code-reviewer (Finding 1) — re-pointed from `spun_off` to
# `backlogged`. DoE's 2026-08-05 ruling relaxed `spun_off` out of the
# pm_approved/grouping-approval gate entirely and gave it its own ungated
# grouping (C3); every test built on this fixture that expects the
# grouping-approval predicate to actually GATE the closed row needs a
# disposition still in `_PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS`, which
# `backlogged` mirrors the migration already done in
# `coordinator_core/ops/tests/test_plan_tasks_mutate.py`. The `spun_off`
# exemption itself is now covered by its own named regression test, below.
_ONE_DEFER = (
    "- id: C1\n"
    "  title: live\n"
    "- id: C2\n"
    "  title: cut\n"
    "  disposition: backlogged\n"
    "  disposition_detail: PM said this belongs in its own plan\n"
    "  disposition_ref: docs/plans/2026-07-29-other.md\n"
)


def _defer_digest(tasks_yaml: str = _ONE_DEFER) -> str:
    return compute_grouping_digest(_rows(tasks_yaml), 'defer')


def _spun_off_block(tasks_yaml: str) -> str:
    """An approved `spun_off` grouping block, digest fresh over `tasks_yaml`.

    Needed by every governed fixture carrying a `spun_off` row since
    2026-08-30: that disposition joined the GOVERNED gate when
    plan.schema.json 2.13.0 brought the fourth grouping key (DR-183).
    """
    digest = compute_grouping_digest(_rows(tasks_yaml), 'spun_off')
    return (
        "  spun_off:\n"
        "    status: approved\n"
        "    approver: pm\n"
        "    approved_at: 2026-07-29\n"
        "    pm_utterance: 'yes, cut C2 - it is its own plan'\n"
        f"    digest: '{digest}'\n"
    )


def _governed_fm(
    *,
    grouping: str = 'defer',
    status: str = 'approved',
    digest: str | None = None,
    version: str = '1.2.0',
) -> str:
    digest_value = digest if digest is not None else _defer_digest()
    return (
        f"schema_version: '{version}'\n"
        "grouping_approvals:\n"
        f"  {grouping}:\n"
        f"    status: {status}\n"
        "    approver: pm\n"
        "    approved_at: 2026-07-29\n"
        "    pm_utterance: 'yes, cut C2 — it is its own plan'\n"
        f"    digest: '{digest_value}'\n"
    )


class TestGovernedDiscriminator:
    """Presence of the `grouping_approvals` KEY is the whole discriminator
    (2026-07-29 correction). An earlier version of this predicate also
    required `schema_version >= (1, 2)` — that conjunct had no producer
    anywhere in this repo (no plan schema declares `schema_version`), so it
    was always unsatisfied and `is_governed_plan` returned False for every
    plan that will ever be authored here, including a fully-approved one.
    See cross-repo/inbox/2026-07-29-doe-claude-em-grouping-discriminator-correction.md.
    """

    def test_no_block_is_legacy(self):
        assert is_governed_plan({'schema_version': '1.2.0'}) is False

    def test_block_present_is_governed_regardless_of_version(self):
        """Presence alone IS sufficient — no version conjunct, and an
        absent/old/malformed `schema_version` must not demote a plan that
        carries the block back to legacy."""
        assert is_governed_plan({'grouping_approvals': {}}) is True
        assert is_governed_plan(
            {'schema_version': '1.1.1', 'grouping_approvals': {'defer': {}}}
        ) is True
        assert is_governed_plan(
            {'schema_version': 'not-a-version', 'grouping_approvals': {'defer': {}}}
        ) is True

    def test_block_and_current_version_is_governed(self):
        assert is_governed_plan(
            {'schema_version': '1.2.0', 'grouping_approvals': {'defer': {}}}
        ) is True

    def test_empty_dict_block_reads_governed_not_legacy(self):
        """An empty `grouping_approvals: {}` is a malformed GOVERNED plan
        (every grouping unapproved), never a legacy plan — the predicate
        (`check_plan_tasks_grouping_approval`) is responsible for failing
        loud on it, not `is_governed_plan` demoting it back to legacy."""
        assert is_governed_plan({'grouping_approvals': {}}) is True


class TestLegacyUnchanged:
    def test_legacy_plan_not_checked_by_predicate(self):
        source = _plan(_ONE_DEFER)
        assert check_plan_tasks_grouping_approval(source) is None

    def test_legacy_row_rule_still_demands_pm_approved(self):
        """The per-row gate at schema_validate.py's row rule is untouched for
        the legacy corpus — DoE asked for exactly this and every existing
        `pm_approved: true` row in both corpora depends on it.

        Review: code-reviewer (Finding 1) — re-pointed from `spun_off` to
        `backlogged`: DoE's 2026-08-05 ruling relaxed `spun_off` out of this
        gate entirely, so it no longer demonstrates the gate firing.
        """
        row = {
            'id': 'C2',
            'disposition': 'backlogged',
            'disposition_detail': 'because',
            'disposition_ref': 'docs/plans/x.md',
        }
        error = _cf_plan_tasks_disposition_shape(row)
        assert error is not None
        assert error['field'] == 'pm_approved'

    def test_governed_suppresses_the_row_leg(self):
        """On a governed plan the per-row boolean is not the authorization
        signal, so leaving this leg live would reject every closed row."""
        row = {
            'id': 'C2',
            'disposition': 'backlogged',
            'disposition_detail': 'because',
            'disposition_ref': 'docs/plans/x.md',
            'case_against': 'because',
        }
        assert _cf_plan_tasks_disposition_shape(row, governed=True) is None

    def test_spun_off_never_requires_pm_approved(self):
        """Named regression pin for DoE's 2026-08-05 ruling (C3/C8): `spun_off`
        is CLOSED but requires NO pm_approved in legacy mode, unlike
        `backlogged`/`wont_do` above. C8's commit message claimed this
        coverage existed; it did not — this is the first named test for it.
        """
        row = {
            'id': 'C2',
            'disposition': 'spun_off',
            'disposition_detail': 'because',
            'disposition_ref': 'docs/plans/x.md',
        }
        assert _cf_plan_tasks_disposition_shape(row) is None

    def test_governed_still_enforces_ref_shape(self):
        """Suppression covers the pm_approved leg ONLY — D2 ref-shape and
        every shared-helper leg still apply on a governed plan."""
        row = {
            'id': 'C2',
            'disposition': 'coded',
            'disposition_ref': 'not-a-sha',
        }
        assert _cf_plan_tasks_disposition_shape(row, governed=True) is not None


class TestPredicate:
    def test_approved_with_fresh_digest_admits(self):
        source = _plan(_ONE_DEFER, frontmatter=_governed_fm())
        assert check_plan_tasks_grouping_approval(source) is None

    def test_pending_grouping_rejects(self):
        source = _plan(_ONE_DEFER, frontmatter=_governed_fm(status='pending'))
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert error['field'] == 'grouping_approvals.defer'
        assert "'C2'" in error['error']

    def test_absent_block_for_that_grouping_rejects(self):
        """No partial legacy tolerance on a governed plan: a row whose
        grouping has no block at all is refused, not grandfathered."""
        source = _plan(_ONE_DEFER, frontmatter=_governed_fm(grouping='ruled_out'))
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert error['field'] == 'grouping_approvals.defer'

    def test_stale_digest_rejects(self):
        """The row set changed after approval — a widened cut-set.

        Review: code-reviewer (Finding 1) — the added row re-pointed from
        `spun_off` to `backlogged`: `spun_off` now occupies its own
        grouping, so it never widened `defer`'s membership and this
        assertion was vacuously true against an unchanged digest.
        """
        widened = _ONE_DEFER + (
            "- id: C3\n"
            "  title: also cut\n"
            "  disposition: backlogged\n"
            "  disposition_detail: snuck in after approval\n"
            "  disposition_ref: docs/plans/2026-07-29-other.md\n"
        )
        source = _plan(widened, frontmatter=_governed_fm(digest=_defer_digest()))
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert error['field'] == 'grouping_approvals.defer.digest'

    def test_bare_hex_digest_rejected(self):
        """The sha256: prefix is load-bearing, not cosmetic."""
        bare = _defer_digest().removeprefix('sha256:')
        source = _plan(_ONE_DEFER, frontmatter=_governed_fm(digest=bare))
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert error['field'] == 'grouping_approvals.defer.digest'
        assert 'sha256:' in error['hint']

    def test_spun_off_without_its_grouping_block_is_refused(self):
        """Named regression pin for DR-183 as landed 2026-08-30: a governed
        plan with a closed `spun_off` row and NO `spun_off` grouping_approvals
        block is refused, the same as any other PM-gated disposition whose
        block is absent (`test_absent_block_for_that_grouping_rejects` above).

        This test asserted the OPPOSITE until plan.schema.json 2.13.0 was
        vendored: DoE's 2026-08-05 ruling exempted `spun_off`, and the gate
        could not have been built anyway while `grouping_approvals` declared
        no fourth key to approve. DR-183 (2026-08-29) reversed the ruling and
        the key's arrival unblocked the GOVERNED half; the legacy per-row
        `pm_approved` leg deliberately did not widen with it.
        """
        tasks = (
            "- id: C1\n"
            "  title: live\n"
            "- id: C2\n"
            "  title: cut\n"
            "  disposition: spun_off\n"
            "  disposition_detail: PM said this belongs in its own plan\n"
            "  disposition_ref: docs/plans/2026-07-29-other.md\n"
        )
        # Deliberately no `spun_off` (or `defer`) block in grouping_approvals.
        fm = "schema_version: '1.2.0'\ngrouping_approvals:\n  do:\n    status: pending\n"
        source = _plan(tasks, frontmatter=fm)
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert error['field'] == 'grouping_approvals.spun_off'

    def test_coded_never_gated(self):
        """A shipped row is evidence of work done, not a scope decision.
        Widening the trigger to `coded` would make every ordinary plan
        completion require a PM utterance, and the gate would be disabled
        within a week."""
        tasks = (
            "- id: C1\n"
            "  title: shipped\n"
            "  disposition: coded\n"
            "  disposition_ref: 3e4ea2e1\n"
        )
        fm = "schema_version: '1.2.0'\ngrouping_approvals:\n  do:\n    status: pending\n"
        source = _plan(tasks, frontmatter=fm)
        assert check_plan_tasks_grouping_approval(source) is None

    def test_open_never_gated(self):
        tasks = "- id: C1\n  title: live\n"
        fm = "schema_version: '1.2.0'\ngrouping_approvals:\n  do:\n    status: pending\n"
        source = _plan(tasks, frontmatter=fm)
        assert check_plan_tasks_grouping_approval(source) is None

    def test_governed_closed_row_without_pm_approved_but_with_detail_passes(self):
        """The positive path the schema-filtering fix exists for: a GOVERNED
        plan, `defer` grouping approved with a fresh matching digest, and a
        closed row (`C2`) that carries `disposition_detail` but genuinely has
        NO `pm_approved` key at all — end to end through the combined
        source-scoped door, `check_plan_tasks_source`.

        Exists because the only other test that calls `check_plan_tasks_source`
        (`test_approved_grouping_is_not_sufficient_without_detail`, above)
        asserts the FAILURE case. `error is not None` is satisfiable by
        either the intended failure reason (missing `disposition_detail`) or
        an unintended one (the governed schema filter silently regressing
        and reintroducing the `pm_approved`-required branch) — that test
        cannot tell the two apart. This test pins the success case so a
        regression of that shape turns green into red instead of staying
        invisible.
        """
        tasks = (
            "- id: C1\n"
            "  title: live\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/frontmatter/schema_validate.py\n"
            "- id: C2\n"
            "  title: cut\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/frontmatter/schema_validate.py\n"
            "  disposition: spun_off\n"
            "  disposition_detail: PM said this belongs in its own plan\n"
            "  disposition_ref: docs/plans/2026-07-29-other.md\n"
        )
        source = _plan(
            tasks,
            frontmatter=_governed_fm(digest=_defer_digest(tasks))
            + _spun_off_block(tasks),
        )
        assert check_plan_tasks_source(source) is None

    def test_approved_grouping_is_not_sufficient_without_detail(self):
        """A closed row needs an excellent REASON and recorded ASSENT — two
        distinct requirements in two distinct slots. An approved grouping
        with a missing disposition_detail is still a cut nobody argued for."""
        tasks = (
            "- id: C1\n"
            "  title: live\n"
            "- id: C2\n"
            "  title: cut\n"
            "  disposition: spun_off\n"
            "  disposition_ref: docs/plans/2026-07-29-other.md\n"
        )
        source = _plan(
            tasks,
            frontmatter=_governed_fm(digest=_defer_digest(tasks))
            + _spun_off_block(tasks),
        )
        # The predicate itself is satisfied...
        assert check_plan_tasks_grouping_approval(source) is None
        # ...but the combined source-scoped door still refuses the row.
        error = check_plan_tasks_source(source)
        assert error is not None


class TestMalformedGoverned:
    """A plan carrying `grouping_approvals` with a malformed value is a
    malformed GOVERNED plan (task 1, 2026-07-29 correction) — it must fail
    loud and never silently degrade to the legacy per-row gate."""

    def test_non_dict_grouping_approvals_fails_loud(self):
        source = _plan(
            _ONE_DEFER,
            frontmatter="grouping_approvals: 'not-a-mapping'\n",
        )
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert error['field'] == 'grouping_approvals'
        assert 'mapping' in error['error']

    def test_list_shaped_grouping_approvals_fails_loud(self):
        source = _plan(
            _ONE_DEFER,
            frontmatter="grouping_approvals:\n  - defer\n",
        )
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert error['field'] == 'grouping_approvals'

    def test_empty_grouping_approvals_dict_fails_loud_per_grouping(self):
        """An empty block never legacies out — each closed row's grouping
        reads 'absent' and refuses, exactly the missing-block branch."""
        source = _plan(_ONE_DEFER, frontmatter="grouping_approvals:\n  defer:\n")
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert error['field'] == 'grouping_approvals.defer'
        assert 'PM' in error['hint']


class TestRefusalMessages:
    """The hard requirement: whatever refuses must direct the author to ask
    the PM, and must NOT print a stamp command, a CLI invocation, or any
    other means of satisfying the field without one.

    This is the specific defect being fixed. The retired
    `_PM_APPROVAL_OFFER` was a gate printing its own key — correct refusal
    text, and an offer that handed over the command defeating it.
    """

    @pytest.mark.parametrize(
        'source',
        [
            _plan(_ONE_DEFER, frontmatter=_governed_fm(status='pending')),
            _plan(_ONE_DEFER, frontmatter=_governed_fm(digest='sha256:' + '0' * 64)),
            _plan(_ONE_DEFER, frontmatter=_governed_fm(digest='deadbeef')),
        ],
    )
    def test_refusal_directs_to_pm(self, source):
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert 'PM' in error['hint']

    @pytest.mark.parametrize(
        'source',
        [
            _plan(_ONE_DEFER, frontmatter=_governed_fm(status='pending')),
            _plan(_ONE_DEFER, frontmatter=_governed_fm(digest='sha256:' + '0' * 64)),
            _plan(_ONE_DEFER, frontmatter=_governed_fm(digest='deadbeef')),
        ],
    )
    def test_refusal_offers_no_command(self, source):
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        text = f"{error['error']} {error['hint']}"
        for forbidden in ('--verb', '--updates', 'coordinator-', 'python -m', '$ ', 'run:'):
            assert forbidden not in text, (
                f'refusal text offers a command-shaped remediation ({forbidden!r}) — '
                'that is the gate-prints-its-own-key defect this contract removes'
            )


class TestDigestNegativeSpec:
    """The digest covers the sorted set of (row id, disposition) pairs for one
    grouping, and NOTHING else. These assertions are the contract."""

    def test_row_order_does_not_change_digest(self):
        a = _rows(
            "- id: C2\n  disposition: spun_off\n"
            "- id: C3\n  disposition: backlogged\n"
        )
        b = _rows(
            "- id: C3\n  disposition: backlogged\n"
            "- id: C2\n  disposition: spun_off\n"
        )
        assert compute_grouping_digest(a, 'defer') == compute_grouping_digest(b, 'defer')

    def test_unrelated_row_field_does_not_change_digest(self):
        a = _rows("- id: C2\n  disposition: spun_off\n")
        b = _rows(
            "- id: C2\n"
            "  disposition: spun_off\n"
            "  disposition_detail: an entirely rewritten rationale\n"
            "  owner: someone-else\n"
        )
        assert compute_grouping_digest(a, 'defer') == compute_grouping_digest(b, 'defer')

    def test_pm_approved_does_not_change_digest(self):
        a = _rows("- id: C2\n  disposition: spun_off\n")
        b = _rows("- id: C2\n  disposition: spun_off\n  pm_approved: true\n")
        assert compute_grouping_digest(a, 'defer') == compute_grouping_digest(b, 'defer')

    def test_prose_and_formatting_do_not_change_any_digest(self):
        """Reformatting a section or editing prose leaves EVERY grouping's
        digest unchanged — the whole reason this is not a whole-body hash."""
        plain = _plan(_ONE_DEFER)
        reformatted = _plan(
            _ONE_DEFER,
            prose='Some newly added narrative about the plan.\n\n### A new subsection\n\nMore.\n\n',
        )
        for grouping in ('do', 'defer', 'ruled_out'):
            assert compute_grouping_digest(
                _rows(_ONE_DEFER), grouping
            ) == compute_grouping_digest(_rows(_ONE_DEFER), grouping)
        assert plain != reformatted  # the documents genuinely differ

    def test_adding_a_row_changes_only_its_own_grouping(self):
        before = _rows(
            "- id: C1\n"
            "- id: C2\n  disposition: spun_off\n"
            "- id: C3\n  disposition: wont_do\n"
        )
        after = _rows(
            "- id: C1\n"
            "- id: C2\n  disposition: spun_off\n"
            "- id: C4\n  disposition: backlogged\n"
            "- id: C3\n  disposition: wont_do\n"
        )
        assert compute_grouping_digest(before, 'defer') != compute_grouping_digest(after, 'defer')
        assert compute_grouping_digest(before, 'do') == compute_grouping_digest(after, 'do')
        assert compute_grouping_digest(before, 'ruled_out') == compute_grouping_digest(
            after, 'ruled_out'
        )

    def test_re_dispositioning_changes_both_touched_groupings(self):
        """Review: code-reviewer (Finding 1) — `before` re-pointed from
        `spun_off` to `backlogged`: since DoE's 2026-08-05 ruling gave
        `spun_off` its OWN grouping, it was never in `defer` to begin with,
        so this transition no longer touched `defer` at all and the
        assertion below was vacuously true against the empty-set digest.
        `backlogged` (which IS in `defer`) restores a genuine defer->
        ruled_out transition.
        """
        before = _rows("- id: C1\n- id: C2\n  disposition: backlogged\n")
        after = _rows("- id: C1\n- id: C2\n  disposition: wont_do\n")
        assert compute_grouping_digest(before, 'defer') != compute_grouping_digest(after, 'defer')
        assert compute_grouping_digest(before, 'ruled_out') != compute_grouping_digest(
            after, 'ruled_out'
        )
        assert compute_grouping_digest(before, 'do') == compute_grouping_digest(after, 'do')

    def test_digest_is_prefixed_and_well_formed(self):
        digest = compute_grouping_digest(_rows("- id: C2\n  disposition: spun_off\n"), 'defer')
        assert digest.startswith('sha256:')
        assert len(digest) == len('sha256:') + 64

    def test_empty_grouping_has_a_stable_digest(self):
        rows = _rows("- id: C1\n")
        assert compute_grouping_digest(rows, 'defer') == compute_grouping_digest(rows, 'defer')

    def test_unknown_grouping_raises(self):
        with pytest.raises(ValueError, match='unknown grouping'):
            compute_grouping_digest([], 'deferred')


class TestGateIsReachable:
    """The anti-inert regression (task 4, 2026-07-29). Every fixture above
    that predates this class sets `schema_version` explicitly, so a
    conjunct against it was always satisfied in test and never in life —
    a green suite could not tell a working gate from an unreachable one.
    These fixtures deliberately carry ONLY what a human or the `docgen`
    scaffolder would actually write: a `grouping_approvals` block and
    nothing else. If `is_governed_plan` ever regains an extra required
    field with no producer anywhere in this repo, these fail."""

    def test_plan_with_only_grouping_approvals_reads_governed(self):
        fm = (
            "grouping_approvals:\n"
            "  defer:\n"
            "    status: approved\n"
            "    approver: pm\n"
            "    approved_at: 2026-07-29\n"
            "    pm_utterance: 'yes, cut C2 — it is its own plan'\n"
            f"    digest: '{_defer_digest()}'\n"
        )
        parsed_fm = {
            'grouping_approvals': {
                'defer': {
                    'status': 'approved',
                    'digest': _defer_digest(),
                },
            },
        }
        assert is_governed_plan(parsed_fm) is True

        source = _plan(_ONE_DEFER, frontmatter=fm)
        assert check_plan_tasks_grouping_approval(source) is None

    def test_plan_with_only_grouping_approvals_actually_gates_closed_rows(self):
        """The gate must not just READ governed — an unapproved grouping on
        this same minimal fixture must actually refuse."""
        fm = (
            "grouping_approvals:\n"
            "  defer:\n"
            "    status: pending\n"
        )
        source = _plan(_ONE_DEFER, frontmatter=fm)
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert error['field'] == 'grouping_approvals.defer'



class TestSpunOffGateIsLiveOnTheFourthGroupingKey:
    """DR-183 (2026-08-29) re-gates `spun_off`, reversing the 2026-08-05
    five-exits relaxation. Its claude-klabauter half landed 2026-08-30, when
    plan.schema.json 2.13.0 was vendored and brought
    `grouping_approvals.spun_off` — the fourth key the gate structurally
    needed. Before it, gating `spun_off` rejected every governed plan
    carrying such a row with no author-side remedy: approving the grouping
    was impossible and authoring the block was itself a schema violation.

    SCOPE OF THE WIDEN, decided 2026-08-29 and honoured here: governed only.
    The legacy per-row leg (`_PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS`,
    read by `_cf_plan_tasks_disposition_shape`) stays
    `{'backlogged', 'wont_do'}`, though DoE's ask included it. Widening it
    retroactively invalidates 16 `spun_off` rows across 10 legacy plans,
    7 of them live, written correctly under the 2026-08-05 relaxation; the
    only repair is forging `pm_approved: true` on rows nobody approved. The
    contract's own migration model settles it — a plan joins by acquiring
    the block, and "authoring the block IS the migration event" — so every
    affected plan is pre-contract by construction and DR-183 binds forward.
    """

    def test_the_fourth_grouping_key_has_arrived(self):
        """The vendored schema declares the key the governed widen rests on.
        A red here means a re-vendor dropped it and the widen below is
        gating rows whose approval block can no longer be authored."""
        import json
        from pathlib import Path

        schema_path = (
            Path(__file__).resolve().parents[1] / 'schemas' / 'plan.schema.json'
        )
        blocks = json.loads(schema_path.read_text(encoding='utf-8'))[
            'properties'
        ]['grouping_approvals']

        assert 'spun_off' in blocks['properties'], (
            'plan.schema.json no longer declares a spun_off grouping_approvals '
            'key, but the GOVERNED gate still scans spun_off rows -- a '
            'governed plan with such a row now has no authorable remedy. '
            'Re-vendor from DoE HEAD, or narrow '
            '_PLAN_TASKS_GOVERNED_PM_APPROVAL_GATED_DISPOSITIONS back.'
        )

    def test_a_governed_plan_with_a_spun_off_row_needs_its_grouping_approved(self):
        """The inversion of the pre-2026-08-30 test: `spun_off` is PM-gated on
        a governed plan, so one whose only closed row is `spun_off` fails
        without an approved `spun_off` block."""
        tasks = (
            "- id: C1\n"
            "  disposition: spun_off\n"
            "  disposition_ref: docs/plans/2026-08-29-successor.md\n"
            "  disposition_detail: 'moved to its own plan'\n"
        )
        source = _plan(tasks, frontmatter=_governed_fm())
        error = check_plan_tasks_grouping_approval(source)
        assert error is not None
        assert error['field'] == 'grouping_approvals.spun_off'

    def test_the_legacy_per_row_leg_did_not_widen(self):
        """Governed-only, per DR-183's scope call: the legacy `pm_approved`
        set stays two-valued so the 16 legacy `spun_off` rows are not
        retroactively invalidated."""
        from coordinator_core.frontmatter.schema_validate import (
            _PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS,
        )

        assert _PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS == frozenset(
            {'backlogged', 'wont_do'}
        )

"""
fixture_expectations.py — the single shared expectation table for the
plan `## Tasks`-spine fenced-block LOCATE rule.

Two independent implementations of one locate rule exist:
  - coordinator-claude's `_locate_tasks_block` (coordinator/bin/coordinator-harvest-deferrals:317-372),
    which returns `str | None` (the fence body, or `None` on either failure mode).
  - `coordinator_core.frontmatter.body_blocks.locate_fenced_block`, whose docstring
    claims parity with the former and returns a typed `LocateResult` distinguishing
    `LOCATED` / `ABSENT` / `MALFORMED`.

Commit `08cbf4bd` fixed a real divergence between them (the shared locator was
missing two hardening fixes — HTML-comment blanking and containment-not-adjacency
— and warn-and-skipped on scaffolded plans for months). The root cause was not the
missing code, it was that the two locators had SEPARATE fixture sets, so no test
could ever fail on their disagreement.

This module is the fix for THAT: one table, held in common, that both
`coordinator/bin/tests/test_plan_tasks_spine_and_harvest.py` and
`coordinator_core/frontmatter/tests/test_body_blocks.py` parametrize their locator
tests over. Every fixture referenced here lives in this same directory
(`coordinator/bin/tests/fixtures/plan-tasks-spine/`) — the single canonical corpus
both locators are held to.

Negative-spec: this table exists EXACTLY ONCE in the tree. Do NOT re-declare a
second copy, a locator-specific override dict, or a parallel list of fixture
names in either consuming test file — that reintroduces the exact defect this
module fixes one level up (two tables that agree today and silently drift
tomorrow). A fixture inapplicable to one locator's own scope (see
`malformed-row.md` below) is expressed as a `note` field ON its existing table
entry, never as a separate applicability list.

Translating between the two locators' return shapes is the CALLING test's job,
not this module's: coordinator-claude's locator collapses `ABSENT` and `MALFORMED` into a
single `None` return, so a caller comparing against coordinator-claude's `_locate_tasks_block`
should assert `result is None` iff `FIXTURE_EXPECTATIONS[name].outcome is not
LocateOutcome.LOCATED`, and a caller comparing against
`coordinator_core.frontmatter.body_blocks.LocateStatus` should compare
`.value` directly (the two enums' string values are pinned equal by
construction — see `LocateOutcome` below).

Spec backlink: coordinator_core/frontmatter/body_blocks.py (parity target
docstring), coordinator/bin/coordinator-harvest-deferrals:317-372
(`_locate_tasks_block`), commit 08cbf4bd (the divergence this consolidation
guards against recurring).
"""
from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class LocateOutcome(str, Enum):
    """Locate-rule outcome, string-valued to match
    `coordinator_core.frontmatter.body_blocks.LocateStatus` member-for-member
    (`LOCATED`/`ABSENT`/`MALFORMED`, same `.value` strings) without this
    module importing that class — this table must stay usable standalone by
    either locator's own test file with no cross-package dependency.
    """

    LOCATED = "located"
    ABSENT = "absent"
    MALFORMED = "malformed"


class FixtureExpectation(NamedTuple):
    """One row of the shared table: a fixture's expected locate-rule outcome
    plus free-form provenance/scope notes.

    `note` is where a fixture's applicability quirks live (e.g. a fixture
    that is LOCATED under the locate rule but whose primary purpose is
    testing something else entirely) — see the module docstring's
    negative-spec: this is the field, not a second list.
    """

    outcome: LocateOutcome
    note: str = ""


# Fixture basename -> expected outcome, for every fixture in this directory
# that exercises the LOCATE rule (as opposed to, e.g., a downstream
# row-schema-validation concern that happens to reuse a located fixture).
FIXTURE_EXPECTATIONS: dict[str, FixtureExpectation] = {
    "heading-without-fence.md": FixtureExpectation(
        LocateOutcome.MALFORMED,
        note=(
            "'## Tasks' heading present with no fence inside its section; a "
            "fence exists but under a later, different heading — containment "
            "bounds the search, so this is a genuine MALFORMED, not ABSENT."
        ),
    ),
    "malformed-row.md": FixtureExpectation(
        LocateOutcome.LOCATED,
        note=(
            "Exercises row-schema validation (a deferred row missing "
            "change_kind/surface), NOT the locate rule — the fence itself is "
            "well-formed and singular, so both locators LOCATE it cleanly. "
            "The malformed content is a schema-validation concern one layer "
            "up (see test_malformed_row_fails_schema_validation), inapplicable "
            "to either locator's own scope."
        ),
    ),
    "multiple-fenced-blocks.md": FixtureExpectation(
        LocateOutcome.MALFORMED,
        note="Two REAL (non-comment) fenced blocks anywhere in the document.",
    ),
    "prose-between-heading-and-fence.md": FixtureExpectation(
        LocateOutcome.LOCATED,
        note=(
            "Load-bearing prose between the '## Tasks' heading and the real "
            "fence, bounded by a trailing '## Some Later Section' heading — "
            "the containment-not-adjacency regression fixture (Fix 2)."
        ),
    ),
    "template-comment-with-deferral.md": FixtureExpectation(
        LocateOutcome.LOCATED,
        note=(
            "Unedited coordinator-doc-new template HTML comment (embedding a "
            "literal ```yaml plan-tasks``` string) sits above the real fence "
            "— the HTML-comment-blanking regression fixture (Fix 1)."
        ),
    ),
    "valid-single.md": FixtureExpectation(
        LocateOutcome.LOCATED,
        note=(
            "Minimal single-task fixture with leading ('## Problem') and "
            "trailing ('## Dispatch Ledger') sections — exercises the span-"
            "roundtrip and default-params contracts against a minimal body."
        ),
    ),
    "valid-spine-with-deferrals.md": FixtureExpectation(
        LocateOutcome.LOCATED,
        note=(
            "Full multi-row spine (C1/C2a/C2b/D1/D2/D3) — the harvest-CLI "
            "call-site, ledger-derivation, and schema-conditional fixture."
        ),
    ),
    "zero-blocks-with-deferred-marker.md": FixtureExpectation(
        LocateOutcome.ABSENT,
        note=(
            "No fenced block anywhere, but a bare 'deferred: true' line sits "
            "in the '## Tasks' section — the belt-and-suspenders silent-loss "
            "loud-fail fixture (harvest-CLI-specific escalation, but the "
            "locate rule itself still reports ABSENT)."
        ),
    ),
    "zero-fenced-blocks.md": FixtureExpectation(
        LocateOutcome.ABSENT,
        note="Prose only, no fenced block anywhere in the document.",
    ),
}

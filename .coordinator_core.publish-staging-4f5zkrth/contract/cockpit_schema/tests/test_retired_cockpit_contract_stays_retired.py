"""
coordinator_core.contract.cockpit_schema.tests.test_retired_cockpit_contract_stays_retired
— tripwire on a retirement that ~74 permanent skips rest on.

PURPOSE
DoE retired TWO subdirectories of `coordinator/cockpit-contract/` at 7cca4d4c5
("delete redundant cockpit-contract TS mirror + retire vestigial toolchain",
2026-07-16): `fixtures/` and `src/` (the TS/Zod mirror). The PARENT directory
survives, and so does `schema/` — alongside `conformance/`, `DECISIONS.md` and
`README.md`. `SCHEMA_AVAILABLE` is therefore True and `skip_no_schema` does NOT
fire; its tests run normally and are not part of this retirement.

Getting that boundary wrong is easy and this file exists partly because it was
gotten wrong once already: an "is the whole directory gone?" check passes on a
machine with no clone and fails on every machine that has one. The precise claim
is per-subdirectory, so the assertions below are too.

Consequently `skip_no_fixtures` and `skip_no_ts_mirror` are FALSE on every
machine, and roughly 74 tests skip on every run forever.

A permanently-false `skipif` is dead code wearing a conditional's clothing. The
failure mode it creates is silent in both directions:

  - If the directory NEVER returns, nobody re-reads those skips and the dead legs
    accumulate unexamined.
  - If the directory DOES return — DoE reverses the retirement, vendors the
    fixtures back, or a machine acquires an old checkout — those ~74 tests
    silently begin executing again against a corpus nobody re-verified, and a
    green run is indistinguishable from the green run before it.

This test is the tripwire for the second case, which is the dangerous one. It
asserts the retirement still holds, so a reversal surfaces as ONE loud, named
failure pointing at the decision, rather than as a silent change in what the
suite covers.

NEGATIVE SPEC — this is not a coverage test.
It asserts nothing about cockpit-schema correctness and must never be extended to.
What the retired fixtures guarded that still matters — the emitted bytes
`CLAUDE.md` names as a hard external dependency for DoE's release capability — is
covered by tests that actually RUN: `test_committed_emit_drift`,
`test_emit_line_endings`, `test_contract_version_single_source`. Do not treat this
file as standing in for them.

WHEN THIS FIRES, DO NOT SILENCE IT. The correct response is to decide, once, and
record the decision: either re-enable the cross-language parity leg deliberately
(re-reading what it asserts before trusting it green), or delete the skip-marked
tests and these marks together. Deleting this tripwire to restore a green run is
the one wrong answer — it re-creates exactly the invisible state it exists to end.
"""
from __future__ import annotations

import pytest

from coordinator_core.contract.cockpit_schema.tests.conftest import (
    COCKPIT_CONTRACT_DIR,
    DOE_AVAILABLE,
    FIXTURES_AVAILABLE,
    SCHEMA_AVAILABLE,
    TS_MIRROR_AVAILABLE,
)

_RETIREMENT_COMMIT = "DoE 7cca4d4c5 (2026-07-16)"


def test_retired_subdirs_have_not_returned() -> None:
    """`fixtures/` and `src/` are still absent — the two subdirs 7cca4d4c5 removed."""
    if not DOE_AVAILABLE:
        # No clone resolved, so this machine cannot observe the retirement either
        # way. Distinct from the retirement being violated — assert nothing.
        # Review: coordinator:code-reviewer -- pytest.skip, not a bare return,
        # so a no-clone run reports SKIPPED rather than a zero-assertion PASSED.
        pytest.skip("DoE clone not available on this machine")

    resurrected = sorted(
        name
        for name, is_live in {
            "fixtures/": FIXTURES_AVAILABLE,
            "src/ (TS mirror)": TS_MIRROR_AVAILABLE,
        }.items()
        if is_live
    )
    assert not resurrected, (
        f"cockpit-contract subdir(s) {resurrected} have RETURNED under "
        f"{COCKPIT_CONTRACT_DIR}, but they were retired at {_RETIREMENT_COMMIT} and "
        "this package's skip marks encode that retirement as permanent.\n"
        "Roughly 74 tests are gated on their absence and will now silently start "
        "executing again against a corpus nobody has re-verified — a green run "
        "before and after this change look identical. Decide and record: re-enable "
        "the cross-language parity leg deliberately, or delete the skip-marked tests "
        "and their marks together. Do NOT delete this test to go green — that "
        "restores the invisible state it exists to end."
    )


def test_surviving_cockpit_contract_surface_is_still_present() -> None:
    """The retirement was partial — assert what SURVIVED still does.

    The inverse tripwire. If `schema/` or the parent directory disappears, the
    tests gated on `skip_no_schema` stop running, and they stop running QUIETLY:
    a skip is not a failure, so the suite stays green while its coverage shrinks.
    That is the same silent-shrink hazard as the resurrection case above, pointed
    the other way, and the retirement's own boundary is what makes it checkable.
    """
    if not DOE_AVAILABLE:
        pytest.skip("DoE clone not available on this machine")

    assert COCKPIT_CONTRACT_DIR is not None and COCKPIT_CONTRACT_DIR.is_dir(), (
        f"coordinator/cockpit-contract/ is GONE at {COCKPIT_CONTRACT_DIR}. "
        f"{_RETIREMENT_COMMIT} retired only fixtures/ and src/ — the parent "
        "directory was deliberately kept. Its removal silently disables every "
        "skip_no_schema-gated test without failing anything."
    )
    assert SCHEMA_AVAILABLE, (
        "cockpit-contract/schema/ is GONE. It SURVIVED the "
        f"{_RETIREMENT_COMMIT} retirement, and the tests gated on it are live "
        "coverage, not part of the retired cross-language leg. They are now "
        "skipping silently — a green run that asserts strictly less than it did."
    )

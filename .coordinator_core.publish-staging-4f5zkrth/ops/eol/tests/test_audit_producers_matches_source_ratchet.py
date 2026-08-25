"""coordinator_core.ops.eol.tests.test_audit_producers_matches_source_ratchet
-- discharges AC6 with an artifact, not a promise.

F10 (staff-eng review, 2026-08-20): `coordinator_core/ops/eol/audit_producers.py`
lifts its AST predicate VERBATIM from
`coordinator_core/tests/test_text_mode_writes_pin_newline.py` -- deliberately
duplicated rather than imported, per that ratchet's own "TEST CODE IS OUT OF
SCOPE for production" negative-spec (importing a `test_*` module from
production code is not a shape this repo uses elsewhere). Both docstrings say
"any future edit to the source predicate must be mirrored here," but nothing
enforced that mirror -- "the operator remembers" was the discharging artifact,
which this repo's own north star (CLAUDE.md) names as the thing a rule may
never rest on. A silent divergence (someone widens `_TEXT_WRITE_ATTRS` or
`_SKIP_DIRS` in the ratchet and forgets this op) would leave `eol.audit_
producers` computing the OLD verdict forever: the boot rider keeps writing
it to the sentinel, the doctor probe keeps reporting PASS, and the figure
goes out to eight sibling repos -- all while this repo's own ratchet is red.

This test is the enforcement: it runs BOTH offender-collection paths over
THIS repo (`audit_producers(REPO_ROOT)` and the ratchet's own
`_production_sources`/`_offenders_in` pair) and asserts the two offender SETS
are equal. A predicate edit in one file with no matching edit in the other
fails this test immediately, loudly, on the fast tier -- no boot, no sentinel,
no doctor probe round-trip required to notice.

ONE documented, deliberate exception to the exact-mirror claim (F14, same
review): `audit_producers._SKIP_DIRS` is a SUPERSET of the ratchet's own
`_SKIP_DIRS`, adding `site-packages`/`build`/`dist` -- directories the
ratchet's single fixed target (this repo) never strictly needs to skip today
but an arbitrary caller-supplied `target_root` can carry (this repo's own
CLAUDE.md: "This repo hosts the fleet's venv"). The scanned-count comparison
below accounts for this explicitly rather than either ignoring it (which
would silently blind the test to a REAL `_SKIP_DIRS` divergence elsewhere)
or asserting raw equality (which would false-fail on the intentional
widening, as it does today: this repo currently carries a top-level `dist/`
with production-shaped `.py` files the op correctly skips and the ratchet
does not).

Deliberately imports `coordinator_core.tests.test_text_mode_writes_pin_newline`
as a plain module (not `test_no_production_text_write_omits_newline` itself,
and not via pytest collection) -- this is a TEST FILE reaching into another
test module's private helpers for the comparison, which is a different shape
from PRODUCTION code importing test code (the thing both modules' own
negative-specs forbid); `audit_producers.py` itself imports nothing from
either test module.

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md § C4, AC6
Spec backlink: coordinator_core/tests/test_text_mode_writes_pin_newline.py (predicate source)
"""

from __future__ import annotations

import coordinator_core.tests.test_text_mode_writes_pin_newline as _ratchet
from coordinator_core.ops.eol.audit_producers import audit_producers

#: F14's documented widening beyond the ratchet's own `_SKIP_DIRS` -- see
#: `audit_producers._SKIP_DIRS`'s own comment for the full rationale. Used
#: below to adjust the ratchet's source count for a fair comparison, not to
#: suppress a real divergence.
_EXTRA_SKIP_DIRS_BEYOND_RATCHET = frozenset({"site-packages", "build", "dist"})


def _ratchet_sources_minus_extra_skip_dirs() -> list[tuple[str, str]]:
    """The ratchet's own `_production_sources()`, filtered to exclude
    anything under `_EXTRA_SKIP_DIRS_BEYOND_RATCHET` -- the fair comparison
    set for `eol.audit_producers`, which skips those directories on purpose
    (F14). Filtering here rather than accepting a divergence unconditionally
    means a REAL predicate drift (a change to `_TEXT_WRITE_ATTRS`,
    `_is_test_file`, or any other shared logic) still fails loudly; only the
    documented, intentional `_SKIP_DIRS` superset is absorbed."""
    return [
        (rel, src)
        for rel, src in _ratchet._production_sources()
        if not _EXTRA_SKIP_DIRS_BEYOND_RATCHET.intersection(rel.split("/"))
    ]


def test_audit_producers_offender_set_matches_source_ratchet_over_this_repo():
    """The two independently-maintained offender-collection paths must agree
    over this repo's own corpus -- byte-identical offender SETS, not merely
    the same count (a same-count-different-members mismatch would be a worse
    silent failure than a raw divergence)."""
    ratchet_sources = _ratchet_sources_minus_extra_skip_dirs()
    assert ratchet_sources, "ratchet discovery found no production sources -- the walk is broken"

    ratchet_offenders: set[str] = set()
    for rel, src in ratchet_sources:
        ratchet_offenders.update(_ratchet._offenders_in(src, rel))

    op_result = audit_producers(_ratchet.REPO)
    op_offenders = set(op_result["offenders"])

    assert op_offenders == ratchet_offenders, (
        "eol.audit_producers' offender set diverges from the source ratchet's "
        "own offender set over this repo -- the two predicates have drifted "
        "out of lockstep (see both modules' docstrings on the mirror "
        "requirement).\n"
        f"Only in eol.audit_producers: {sorted(op_offenders - ratchet_offenders)}\n"
        f"Only in the source ratchet:   {sorted(ratchet_offenders - op_offenders)}"
    )


def test_audit_producers_scanned_count_matches_ratchet_source_count():
    """A cheaper, redundant cross-check on the SOURCE SET itself (not just
    the offenders found in it) -- `_SKIP_DIRS`/`_is_test_file` divergence
    beyond the documented F14 widening would pass the test above (if it
    happened to find the same offenders) and fail this one."""
    ratchet_sources = _ratchet_sources_minus_extra_skip_dirs()
    op_result = audit_producers(_ratchet.REPO)

    assert op_result["scanned"] == len(ratchet_sources), (
        f"eol.audit_producers scanned {op_result['scanned']} production sources; "
        f"the source ratchet's own walk, minus the documented F14 "
        f"site-packages/build/dist widening, found {len(ratchet_sources)} -- "
        "_SKIP_DIRS or _is_test_file has diverged between the two modules "
        "beyond the accounted-for F14 exception."
    )

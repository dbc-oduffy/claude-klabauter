"""Designed-red falsifier for the gravestoned review-trail surface's deletion.

Spec backlink: `docs/plans/2026-08-29-the-gravestoned-review-trail-surface-is-deleted.md`
chunk C1. DR-372 (2026-08-27) replaced the per-commit review trail with a binary review
receipt; DR-374 ruled the surviving writer/CLI surface gravestoned -- dead code kept
registered only until a follow-up chunk drains it from every registration surface and
deletes the modules and CLIs outright. This module is that falsifier: it encodes the plan's
prime exit criterion as three assertions rather than a reviewer's grep, so the deletion chunks
(C2-C6) turn it green by actually deleting, not by a human eyeballing a diff.

RED AT AUTHORING TIME, BY CONSTRUCTION. All fourteen files below exist on disk right now;
four registration surfaces still name `review_trail.write` or `review_trail.scan_unresolved_ubt`
(or, for `dispatchable.py`, the CLI-name form `scan_unresolved_ubt_records`); and
`workstream_complete/__init__.py` still calls `build_ubt_pending_check_directive`. Do NOT
weaken any assertion below to make it pass -- if a test here blocks a chunk, the deletion is
wrong or incomplete, not this file.

Negative-spec: this file asserts the WRITER and its CLIs are gone. It does NOT touch, assert
on, or gate `coordinator_core/review_trail/` (the reviewed-set store: `__init__.py`,
`backfill.py`, `receipt_credit.py`, `reviewed_set.py`, `tests/`) -- that package is read live
by `gate_dimension_review.py::_review_dimension_check` and `review_coverage_core.py:126` and
must keep answering the close gate exactly as it does today. It also does not touch
`state/review-trail/`'s 3,662 historical records, which stay exactly as they are per DR-372.
"""

from __future__ import annotations

import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# SSOT: the fourteen files this plan retires. Every later chunk in this plan cites this
# tuple rather than re-enumerating the surface.
RETIRED_REVIEW_TRAIL_FILES: tuple[str, ...] = (
    "coordinator_core/ops/review_trail_write.py",
    "coordinator_core/ops/list_review_trail_records.py",
    "coordinator_core/ops/scan_unresolved_ubt_records.py",
    "coordinator_core/ops/review_trail_readjudication_report.py",
    "coordinator/bin/coordinator-write-review-trail.py",
    "coordinator/bin/coordinator-write-review-trail.cmd",
    "coordinator/bin/coordinator-write-review-trail.ps1",
    "coordinator/bin/list-review-trail-records.py",
    "coordinator/bin/list-review-trail-records.cmd",
    "coordinator/bin/repair-empty-review-trail-ranges.py",
    "coordinator/bin/repair-empty-review-trail-ranges.cmd",
    "coordinator/bin/repair-empty-review-trail-ranges.ps1",
    "coordinator/bin/scan_unresolved_ubt_records.py",
    "coordinator/bin/scan_unresolved_ubt_records.cmd",
)

assert len(RETIRED_REVIEW_TRAIL_FILES) == 14, (
    f"SSOT drift: expected 14 retired files, tuple has {len(RETIRED_REVIEW_TRAIL_FILES)}"
)


#: The one member of the SSOT tuple whose deletion is withheld by C6's uncleared
#: `external_gate` on DoE-claude -- the op is REGISTERED, so a sibling repo can dispatch it,
#: and the memo asking whether anything does has been unanswered since 2026-08-16. Split out
#: of the standing assertion below, NOT exempted from it: it carries the same expectation
#: under `pending_fix`, exactly as the `build_ubt_pending_check_directive` leg does.
GATED_ON_SIBLING_ANSWER: tuple[str, ...] = (
    "coordinator_core/ops/review_trail_readjudication_report.py",
)


def test_no_retired_review_trail_file_remains_on_disk():
    """Every file in `RETIRED_REVIEW_TRAIL_FILES` except the C6-gated member must be gone
    (`git rm`, not a filesystem delete -- see the plan's HARD CONSTRAINTS). Red at authoring
    time: all fourteen existed.
    """
    still_present = [
        rel
        for rel in RETIRED_REVIEW_TRAIL_FILES
        if rel not in GATED_ON_SIBLING_ANSWER and (_REPO_ROOT / rel).exists()
    ]
    assert still_present == [], (
        "retired review-trail files still on disk (expected zero): "
        f"{still_present}"
    )


@pytest.mark.pending_fix
def test_c6_gated_review_trail_file_is_gone_once_the_sibling_answers():
    """The readjudication reporter must go too -- the prime exit criterion is 0 of 14, not
    0 of 13. Marked `pending_fix` because C6 is withheld by an uncleared cross-repo gate, so
    this leg stays open without reading as a regression of the assertion above. Delete the
    marker, not the assertion, when doe-claude-em answers.
    """
    still_present = [
        rel for rel in GATED_ON_SIBLING_ANSWER if (_REPO_ROOT / rel).exists()
    ]
    assert still_present == [], (
        "C6-gated review-trail files still on disk (expected zero once the gate clears): "
        f"{still_present}"
    )


def test_no_registration_surface_names_the_retired_ops():
    """Six registration surfaces, not five, must stop naming the retired ops once deletion
    lands: `_registry_map.py`, `authz/classification.py`, `authz/registration_quad.py`,
    `ops/__init__.py`, the `_EAGER_OP_MODULES` list (inside `ops/__init__.py`), and
    `authz/dispatchable.py`'s `ASSEMBLER_DISPATCHABLE["workstream_complete"]` (which names
    the retired scanner by its CLI-dispatch name, `scan_unresolved_ubt_records`, not its op
    id). Red today: `review_trail.scan_unresolved_ubt` is registered in
    `_registry_map.py`, `classification.py`, `registration_quad.py`, and `ops/__init__.py`;
    `scan_unresolved_ubt_records` is named in `dispatchable.py`.
    """
    registry_map = (_REPO_ROOT / "coordinator_core/ops/_registry_map.py").read_text(encoding="utf-8")
    classification = (_REPO_ROOT / "coordinator_core/authz/classification.py").read_text(encoding="utf-8")
    registration_quad = (_REPO_ROOT / "coordinator_core/authz/registration_quad.py").read_text(encoding="utf-8")
    ops_init = (_REPO_ROOT / "coordinator_core/ops/__init__.py").read_text(encoding="utf-8")
    dispatchable = (_REPO_ROOT / "coordinator_core/authz/dispatchable.py").read_text(encoding="utf-8")

    op_ids = ("review_trail.write", "review_trail.scan_unresolved_ubt")

    offenders = []
    for label, text in (
        ("_registry_map.py", registry_map),
        ("authz/classification.py", classification),
        ("authz/registration_quad.py", registration_quad),
        ("ops/__init__.py (incl. _EAGER_OP_MODULES)", ops_init),
    ):
        for op_id in op_ids:
            # Match the quoted op-id literal, not history comments/prose about the op.
            if f'"{op_id}"' in text or f"'{op_id}'" in text:
                offenders.append(f"{label} names {op_id!r}")

    # dispatchable.py's ASSEMBLER_DISPATCHABLE names the CLI-dispatch name, not the op id.
    if '"scan_unresolved_ubt_records"' in dispatchable:
        offenders.append(
            'authz/dispatchable.py ASSEMBLER_DISPATCHABLE names "scan_unresolved_ubt_records"'
        )

    assert offenders == [], (
        "registration surfaces still name a retired review-trail op (expected none): "
        f"{offenders}"
    )


@pytest.mark.pending_fix
def test_workstream_complete_does_not_call_ubt_pending_check_directive():
    """`workstream_complete/__init__.py` must stop calling
    `directives_review.build_ubt_pending_check_directive` once the scanner it feeds is
    deleted. Marked `pending_fix`, not the standing gate: this call site is drained by a
    later chunk in this plan (leg 3), and until that lands this assertion stays open without
    reading as a regression of the two assertions above. Red today: the call site is live at
    `workstream_complete/__init__.py`.
    """
    init_text = (_REPO_ROOT / "coordinator_core/workstream_complete/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "build_ubt_pending_check_directive" not in init_text, (
        "workstream_complete/__init__.py still calls "
        "directives_review.build_ubt_pending_check_directive (expected zero call sites)"
    )

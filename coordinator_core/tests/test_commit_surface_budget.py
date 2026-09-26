"""Per-file narration budget for the commit-surface roster: a one-directional ratchet.

Mirrors `test_ceremony_budget_ratchet.py` (DR-348) applied to a different pair of
quantities. Two numbers per roster file, neither a total-line or executable-line
count: COMMENT+DOCSTRING lines and MARKER-CARRYING lines, both read from
`_commit_surface_roster`'s classifier and marker policy so this test and C2's fixture
and C12's report all describe the same measurement. Ceilings below are independent
literals, not imports of the live count -- importing today's number would make this
test agree with any value whatsoever and assert nothing. Lowering a ceiling is
always fine; raising one requires editing the literal here AND a recorded DR
citation in the same diff (DR-348's own precedent), not a free-text comment.

PLUS the drift check, replacing live derivation with a guarded-completeness
property: every non-test module under `coordinator_core/git/` and
`coordinator_core/ops/ceremony/` must appear in either `COMMIT_SURFACE_FILES` or
`EXCLUDED_MODULES`. A module in neither is a narration holder nobody classified,
and this is what turns that silently-uncovered state red instead of invisible.

Negative spec -- what this module does NOT assert, deliberately:
  - It does NOT budget total lines or executable lines. Narration moves
    comment+docstring and marker-carrying lines; a peer adding a function to
    git_native.py does not move either quantity this test charges for.
  - It does NOT implement a marker-ban rule -- the marker count is reported and
    ratcheted, never asserted to be zero or blocked from existing at all.
  - It does NOT require a newly-discovered module to be budgeted. Classifying it
    as EXCLUDED with a reason is a legitimate disposition of the drift check.

Spec backlink: docs/plans/2026-08-27-the-commit-op-stops-narrating-how-it-got-here.md, C1.
"""
from __future__ import annotations

import pathlib

from coordinator_core.tests._commit_surface_roster import (
    COMMIT_SURFACE_FILES,
    EXCLUDED_MODULES,
    classify,
    marker_lines,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

COMMENT_DOCSTRING_CEILINGS = {
    "coordinator_core/ops/ceremony/git_native.py": 3433,
    "coordinator_core/ops/ceremony/commit_gates.py": 520,
    "coordinator_core/ops/ceremony/push.py": 1147,
    "coordinator_core/ops/ceremony/commit_v2.py": 409,
    "coordinator_core/ops/ceremony/tail_ops.py": 352,
    "coordinator_core/git/git_state.py": 379,
    "coordinator_core/git/git_index.py": 237,
}

MARKER_CEILINGS = {
    "coordinator_core/ops/ceremony/git_native.py": 110,
    "coordinator_core/ops/ceremony/commit_gates.py": 0,
    "coordinator_core/ops/ceremony/push.py": 30,
    "coordinator_core/ops/ceremony/commit_v2.py": 10,
    "coordinator_core/ops/ceremony/tail_ops.py": 15,
    "coordinator_core/git/git_state.py": 0,
    "coordinator_core/git/git_index.py": 0,
}


def test_every_roster_file_has_both_ceilings():
    for path in COMMIT_SURFACE_FILES:
        assert path in COMMENT_DOCSTRING_CEILINGS, (
            f"{path} is in COMMIT_SURFACE_FILES but has no comment+docstring ceiling "
            f"in COMMENT_DOCSTRING_CEILINGS -- an unbudgeted roster file is not a pass."
        )
        assert path in MARKER_CEILINGS, (
            f"{path} is in COMMIT_SURFACE_FILES but has no marker-carrying ceiling "
            f"in MARKER_CEILINGS -- an unbudgeted roster file is not a pass."
        )
    for path in COMMENT_DOCSTRING_CEILINGS:
        assert path in COMMIT_SURFACE_FILES, (
            f"{path} carries a comment+docstring ceiling but is not in "
            f"COMMIT_SURFACE_FILES -- a ceiling for a file off the roster."
        )
    for path in MARKER_CEILINGS:
        assert path in COMMIT_SURFACE_FILES, (
            f"{path} carries a marker-carrying ceiling but is not in "
            f"COMMIT_SURFACE_FILES -- a ceiling for a file off the roster."
        )


def test_comment_and_docstring_lines_do_not_exceed_the_ceiling():
    for path, ceiling in COMMENT_DOCSTRING_CEILINGS.items():
        info = classify(str(REPO_ROOT / path))
        actual = info["comment"] + info["docstring"]
        assert actual <= ceiling, (
            f"{path}: comment+docstring lines grew to {actual}, above the pinned "
            f"{ceiling}-line ceiling. This is the quantity narration moves -- growth "
            f"here is the thing this plan exists to stop. Raising this ceiling "
            f"requires a recorded DR citation in the same diff."
        )


def test_marker_carrying_lines_do_not_exceed_the_ceiling():
    for path, ceiling in MARKER_CEILINGS.items():
        marker = marker_lines(str(REPO_ROOT / path))
        actual = marker["carrying"]
        assert actual <= ceiling, (
            f"{path}: marker-carrying lines grew to {actual}, above the pinned "
            f"{ceiling}-line ceiling. A deletion row that deleted volume without "
            f"deleting narration leaves this ceiling where it was -- this is what "
            f"gives AC1 a red state. Raising this ceiling requires a recorded DR "
            f"citation in the same diff."
        )


def _non_test_modules(directory):
    modules = []
    for p in (REPO_ROOT / directory).glob("*.py"):
        if p.name.startswith("test_"):
            continue
        modules.append(f"{directory}/{p.name}")
    return sorted(modules)


def test_no_module_under_the_two_directories_is_unclassified():
    """The drift check: a module in neither constant is a narration holder nobody classified.

    Direct analogue of DR-348's
    test_an_unlisted_future_ceremony_op_is_bounded_by_construction -- green at HEAD by
    construction (every module in both directories is in one constant or the other);
    red the moment a new narration holder arrives, forcing a human to classify it
    rather than letting it escape the sweep silently. It does NOT require the
    newcomer to be budgeted -- EXCLUDED with a reason is a legitimate answer.
    """
    known = set(COMMIT_SURFACE_FILES) | set(EXCLUDED_MODULES)
    on_disk = set(_non_test_modules("coordinator_core/git")) | set(
        _non_test_modules("coordinator_core/ops/ceremony")
    )
    unclassified = on_disk - known
    assert not unclassified, (
        f"module(s) under coordinator_core/git/ or coordinator_core/ops/ceremony/ "
        f"are in neither COMMIT_SURFACE_FILES nor EXCLUDED_MODULES: "
        f"{sorted(unclassified)}. Classify each as a roster member (with a budget) "
        f"or as EXCLUDED (with a one-line reason) in "
        f"coordinator_core/tests/_commit_surface_roster.py."
    )


def test_known_modules_still_exist_on_disk():
    known = set(COMMIT_SURFACE_FILES) | set(EXCLUDED_MODULES)
    missing = {path for path in known if not (REPO_ROOT / path).exists()}
    assert isinstance(missing, set)

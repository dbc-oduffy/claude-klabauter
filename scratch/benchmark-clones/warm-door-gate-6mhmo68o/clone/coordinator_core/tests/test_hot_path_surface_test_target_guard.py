"""Guard: a named hot-path source surface must resolve to a runnable test target.

Purpose
-------
`coordinator/bin/publish.py` maps to NO test target under DoE-claude's
`emit-dispatch-workflow.py::_map_written_path_to_test_target` stem convention
(`tests/test_<stem>.py`) -- even though publish.py has dozens of topic-scoped
`test_publish_<topic>.py` files that DO cover it. Because the emitter's
terminal-test-phase derivation resolves per-STEM, not by scanning for files
that merely happen to cover a surface, no dispatch wave that touched
publish.py ever selected a test to run over the change. Two real defects
shipped through that hole on 2026-08-26 (see debt-backlog row
`state/debt-backlog/2026-08-26-four-token-index-acs-are-code-only-no-te-
c7a03bd3079e.yaml`): `dispatch_preswap_payload_parity_gate` was re-wired to
take a `token_index_path` kwarg its signature never accepted (`TypeError` on
every call), and the token index's writer/reader root mismatch silently
forced a 1250ms full-scan fallback for any subdirectory dest.

This guard is deliberately narrow: it does not sweep every historical plan.
`emit-dispatch-workflow.py` lives in a sibling repo (DoE-claude) and is not
ours to change, and a repo-wide sweep of every plan/surface would go red
across a lot of pre-existing work nobody has bandwidth to close today --
exactly the shape of guard that gets marked `designed_red` or deleted, and
then protects nothing. Instead it holds a short, named allowlist of
hot-path surfaces to the SAME resolution rule the emitter uses
(`_map_written_path_to_test_target`), with one escape hatch: an explicit
`DECLARED_UNTESTED` entry carrying a written, non-trivial reason. Adding a
hot-path surface without EITHER a resolvable test target OR a declared,
reasoned exemption is what this test forbids -- a surface silently added
with neither is exactly how the two defects above shipped unseen.

Negative-spec:
  - Does NOT sweep every plan or every file under `coordinator/bin/` --
    only the named `HOT_PATH_SURFACES` allowlist. Widening that allowlist
    is a deliberate, per-surface decision, not this test's job.
  - Does NOT alter, patch, or import from DoE-claude's
    `emit-dispatch-workflow.py`. It re-imports the resolution function from
    THIS repo's own `coordinator_core.ops.dispatch_emit.pathspec`, which is
    the seam the DoE shim delegates to unchanged for this specific
    derivation (see that module's own docstring, "Everything in that
    pipeline works against a DoE spine EXCEPT the terminal test phase").
  - Does NOT assert that a resolved test target's suite passes -- only that
    a target exists on disk and is non-trivial (contains at least one
    `def test_`). Running the target's own suite is the terminal test
    phase's job, not this guard's.
  - A `DECLARED_UNTESTED` entry with an empty, missing, or too-short
    `reason` does NOT satisfy the exemption -- it still fails, loudly,
    naming the surface. The point is forcing the reason onto disk, not
    forcing a green run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import pathspec

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Hot-path surfaces this guard holds to a runnable-test-target bar. Narrow
# by design -- add a surface here only when it is genuinely hot-path (a
# safety/publish-gate consumer whose silent breakage is expensive), not as
# a general-purpose sweep target.
HOT_PATH_SURFACES: tuple[str, ...] = (
    "coordinator/bin/publish.py",
)

# Surfaces in HOT_PATH_SURFACES that are KNOWN not to resolve to a test
# target under the stem convention, with the reason written down. Presence
# here does not mean untested -- it means the emitter's stem-based
# derivation cannot discover the tests that already exist for it. Each
# entry is a standing debt-backlog item, not a permanent waiver: shrinking
# this dict as gaps close is expected, growing it silently is what
# `test_declared_untested_reasons_are_non_trivial` and
# `test_declared_untested_entries_are_all_hot_path_surfaces` exist to catch.
DECLARED_UNTESTED: dict[str, str] = {
    "coordinator/bin/publish.py": (
        "dozens of topic-scoped coordinator/bin/tests/test_publish_*.py files "
        "cover this module, but the stem convention only recognizes a single "
        "tests/test_publish.py -- none of the existing files match it, so no "
        "dispatch wave ever selects a test for a change here. Tracked: "
        "state/debt-backlog/2026-08-26-four-token-index-acs-are-code-only-no-te-"
        "c7a03bd3079e.yaml (proposed_action: make a chunk whose surface has no "
        "runnable test target an emitter refusal or an explicit "
        "declared-untested disposition -- this allowlist is that disposition "
        "on OUR side until the emitter, owned by DoE-claude and not ours to "
        "change, grows one of its own)."
    ),
}


def _resolved_test_target(surface: str) -> str | None:
    return pathspec._map_written_path_to_test_target(  # noqa: SLF001 - engine seam, read-only
        surface, repo_root=_REPO_ROOT, declared=frozenset()
    )


@pytest.mark.parametrize("surface", HOT_PATH_SURFACES)
def test_hot_path_surface_has_test_target_or_declared_reason(surface: str) -> None:
    target = _resolved_test_target(surface)
    if target is not None:
        target_path = _REPO_ROOT / target
        assert target_path.is_file(), (
            f"{surface} resolves to test target {target!r} via the emitter's "
            "own stem convention, but that file does not exist on disk."
        )
        body = target_path.read_text(encoding="utf-8")
        assert "def test_" in body, (
            f"{surface}'s resolved test target {target!r} exists but declares "
            "no test function -- a hollow stub is indistinguishable from no "
            "coverage at all."
        )
        return

    reason = DECLARED_UNTESTED.get(surface)
    assert reason, (
        f"{surface} is a declared hot-path surface (HOT_PATH_SURFACES) that "
        "maps to NO runnable test target under "
        "coordinator_core.ops.dispatch_emit.pathspec."
        "_map_written_path_to_test_target's tests/test_<stem>.py convention, "
        "and it carries no DECLARED_UNTESTED reason. This is exactly the gap "
        "that let dispatch_preswap_payload_parity_gate's TypeError and the "
        "token-index root mismatch ship uncaught -- either add a matching "
        "tests/test_<stem>.py for it, or add a DECLARED_UNTESTED[...] entry "
        "naming why not."
    )


def test_declared_untested_entries_are_all_hot_path_surfaces() -> None:
    """An orphaned waiver -- a DECLARED_UNTESTED entry for a surface no
    longer in HOT_PATH_SURFACES -- is dead weight nothing checks against."""
    for surface in DECLARED_UNTESTED:
        assert surface in HOT_PATH_SURFACES, (
            f"{surface!r} is DECLARED_UNTESTED but not in HOT_PATH_SURFACES."
        )


def test_declared_untested_reasons_are_non_trivial() -> None:
    # Review: coordinator:code-reviewer (slice D, Finding 1) -- length alone
    # lets a future author waive a genuinely untested surface with 40
    # characters of plausible filler. Requiring a real
    # state/debt-backlog/*.yaml path -- verified to EXIST on disk, not just
    # pattern-shaped -- raises the bar from "any reason" to "a reason with a
    # tracked follow-up," without loosening the pre-existing length check.
    import re

    debt_backlog_ref = re.compile(r"state/debt-backlog/[\w.-]+\.ya?ml")
    for surface, reason in DECLARED_UNTESTED.items():
        stripped = reason.strip()
        assert len(stripped) >= 40, (
            f"DECLARED_UNTESTED[{surface!r}]'s reason is too short to carry a "
            "real explanation -- write down WHY, not just THAT."
        )
        match = debt_backlog_ref.search(stripped)
        assert match, (
            f"DECLARED_UNTESTED[{surface!r}]'s reason does not cite a "
            "state/debt-backlog/*.yaml path -- a waiver must name a tracked "
            "follow-up, not just an explanation."
        )
        debt_path = _REPO_ROOT / match.group(0)
        assert debt_path.is_file(), (
            f"DECLARED_UNTESTED[{surface!r}]'s reason cites "
            f"{match.group(0)!r}, but that debt-backlog file does not exist "
            "on disk -- the waiver is not backed by a real tracked item."
        )

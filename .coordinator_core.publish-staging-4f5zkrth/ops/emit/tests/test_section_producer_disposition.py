"""Guard: a `sections/*.py` producer module cannot leave the tree silently.

The `artifact.emit` cut (DR-351) deleted the aggregate that used to consume every producer
in `sections/`; nothing forces a future contributor to check for a `query-*` consumer before
deleting one of the fourteen that lack one (`docs/architecture/systems/emit-engine.md` § 1
enumerates them). This test pins the producer roster and requires a same-commit disposition
for any name that disappears from disk — no ceremony, one dict entry naming where it went.

Mechanism mirrors `write_guards/tests/test_guard_registry_manifest.py`: pin an expected set,
compare it to what is actually on disk, fail loudly on divergence. The only addition here is
that a *removal* is allowed when (and only when) `_RETIRED_PRODUCERS` names it.

Maintenance: adding a producer module means adding its name to `_KNOWN_PRODUCERS` in the same
commit. Removing one means moving its name from `_KNOWN_PRODUCERS` to `_RETIRED_PRODUCERS`
with a one-line reason, in the same commit that deletes the file.
"""

from __future__ import annotations

from pathlib import Path

_SECTIONS_DIR = Path(__file__).resolve().parent.parent / "sections"

_NON_PRODUCER_STEMS = frozenset({"__init__", "_shared"})

# The 21 producer modules remaining after the 2026-08-23 file_attribution retirement. Eight
# have a non-test consumer (commit_closures, goals, handoff_columns, initiatives, review_trail,
# rollups, routine_signals, trackers); thirteen do not yet — see emit-engine.md § 1 for the
# full list and the disposition rule this guard enforces.
_KNOWN_PRODUCERS: frozenset[str] = frozenset(
    {
        "backlogs",
        "branch",
        "commit_closures",
        "coordinator_roots",
        "cross_repo_memos",
        "decision_guides",
        "exec_summary",
        "goals",
        "handoff_columns",
        "handoffs",
        "health",
        "initiatives",
        "lessons",
        "plans",
        "review_trail",
        "roadmap_dag",
        "roadmaps",
        "rollups",
        "routine_signals",
        "session_hierarchy",
        "trackers",
    }
)

# name -> one-line disposition ("where it went"), filled in by the commit that deletes the
# module.
_RETIRED_PRODUCERS: dict[str, str] = {
    "file_attribution": (
        "retired outright, no successor — opticon's DROP (2026-08-22, superseding DR-021) "
        "removed the only named consumer's ask, and the entity had zero non-test consumers "
        "in this repo; see cross-repo/inbox/2026-08-23-project-opticon-em-file-attributions-"
        "is-dropped-your-deliberation-rests-on-a-superseded-record.md"
    ),
}


def _disk_producer_stems(sections_dir: Path) -> frozenset[str]:
    return frozenset(
        p.stem for p in sections_dir.glob("*.py") if p.stem not in _NON_PRODUCER_STEMS
    )


def _undisposed_removals(
    known: frozenset[str], retired: dict[str, str], disk: frozenset[str]
) -> list[str]:
    """Names in `known` that are neither on disk nor accounted for in `retired`."""
    return sorted(name for name in known if name not in disk and name not in retired)


def _untracked_additions(known: frozenset[str], disk: frozenset[str]) -> list[str]:
    return sorted(disk - known)


def test_producer_removal_requires_disposition() -> None:
    violations = _undisposed_removals(
        _KNOWN_PRODUCERS, _RETIRED_PRODUCERS, _disk_producer_stems(_SECTIONS_DIR)
    )
    assert not violations, (
        f"producer(s) removed with no disposition: {violations}. "
        "Add a _RETIRED_PRODUCERS entry naming where it went, in the removing commit."
    )


def test_disk_producers_match_known_roster() -> None:
    additions = _untracked_additions(_KNOWN_PRODUCERS, _disk_producer_stems(_SECTIONS_DIR))
    assert not additions, (
        f"producer(s) not tracked in this guard: {additions}. "
        "Add the name to _KNOWN_PRODUCERS in this commit."
    )


# ---------------------------------------------------------------------------
# Pure-function coverage for the check itself — synthetic input only, never the real tree.
# ---------------------------------------------------------------------------


def test_check_fires_red_when_a_known_producer_vanishes_without_disposition() -> None:
    known = frozenset({"alpha", "beta"})
    disk = frozenset({"alpha"})  # "beta" deleted, no disposition entry
    assert _undisposed_removals(known, {}, disk) == ["beta"]


def test_check_goes_green_once_the_disposition_is_recorded() -> None:
    known = frozenset({"alpha", "beta"})
    disk = frozenset({"alpha"})  # "beta" still gone
    retired = {"beta": "folded into alpha.py's collect() — 2026-08-23"}
    assert _undisposed_removals(known, retired, disk) == []


def test_check_ignores_untracked_additions_for_the_removal_leg() -> None:
    known = frozenset({"alpha"})
    disk = frozenset({"alpha", "gamma"})  # new module, not yet in the roster
    assert _undisposed_removals(known, {}, disk) == []
    assert _untracked_additions(known, disk) == ["gamma"]

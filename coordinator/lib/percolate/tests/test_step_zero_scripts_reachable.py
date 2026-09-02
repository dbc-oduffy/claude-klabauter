"""C8 (docs/plans/2026-09-01-the-dogfooded-install-stops-lying-about.md) --
`chain-walk.py`, `normalize-env.py`, `install-maximalist.py` (all three live
under `coordinator/scripts/`) are authoring-only: verified against
`setup/publish-targets.portable`'s real target/allowlist resolution, never
against source-file existence or reachability alone.

Ledger item 1 (F-008). The decision (recorded in this chunk's dispatch brief,
`state/dispatch-briefs/2026-09-01-the-dogfooded-install-stops-lying-about/C8.md`)
is DECIDED authoring-only -- these three scripts are not published, and
nothing published may name them as reachable either. This test is the
closure evidence: the PROJECTION (what `setup/publish-targets.portable`'s
rows actually resolve into), not the commit that landed the scripts or a
grep of DoE's INSTALL.md prose.

Mechanism (uses `coordinator.lib.percolate.targets.parse_portable_rows`, the
same shared parser `test_post_transform_projection_parses.py` calls --
Review: overengineering-reviewer -- rather than each test module keeping its
own field-indexed copy):

  1. Parse every row of `setup/publish-targets.portable` into
     `(name, source_subdir, allowlist)`.
  2. Assert no row's `source_subdir` is `coordinator/scripts` (or any path
     that would resolve TO it) -- i.e. no row roots a publish at the
     directory these three scripts live in.
  3. Assert no row's allowlist names any of the three scripts by basename
     (a row could in principle allowlist a `coordinator/scripts/<file>`-relative
     entry from an unrelated `source_subdir` via `source_map`; scanning every
     row's allowlist, not just rows rooted at `coordinator/scripts`, closes
     that gap too).
  4. Pin the one row that DOES publish `scripts/setup.py`'s own directory
     (`claude-klabauter-scripts`, source_subdir `scripts` -- this repo's
     TOP-LEVEL `scripts/`, a different directory from `coordinator/scripts/`)
     to its known-correct, three-entry allowlist (`setup.py,setup.cmd,test_setup.py`)
     -- catching the exact half-named-path bug this ledger item describes if
     anyone ever widens that allowlist to include the three coordinator/scripts
     entries by mistake.

Negative-spec: does NOT reach into DoE-claude's mirror or source tree (that
half of item 1 -- confirming DoE's C6 strip actually landed in the published
`commands/install.md` -- is DoE's own closure evidence, sequenced explicitly
AFTER their percolate round per this chunk's dispatch brief; this test's
scope is claude-klabauter's own publish-surface row set only). Does NOT assert file
existence/non-existence of the three scripts on disk -- their presence
under `coordinator/scripts/` is not in question; whether the publish
topology reaches them is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator.lib.percolate.targets import parse_portable_rows as _parse_portable_rows

pytestmark = [pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PORTABLE_TARGETS_PATH = _REPO_ROOT / "setup" / "publish-targets.portable"

# The three Step Zero scripts this ledger item is about -- all live under
# coordinator/scripts/, none under top-level scripts/ (scripts/setup.py's
# own directory, which IS published via the claude-klabauter-scripts row).
_STEP_ZERO_SCRIPTS = ("chain-walk.py", "normalize-env.py", "install-maximalist.py")

_UNPUBLISHED_SOURCE_SUBDIR = "coordinator/scripts"

# `_parse_portable_rows` moved to `coordinator.lib.percolate.targets ::
# parse_portable_rows` (Review: overengineering-reviewer -- this module and
# `test_post_transform_projection_parses.py` each carried their own copy of
# the same field-indexed parser for one on-disk row format; both now import
# the shared production-side helper instead).


def test_portable_targets_file_exists():
    assert _PORTABLE_TARGETS_PATH.is_file(), (
        f"{_PORTABLE_TARGETS_PATH} not found -- cannot evaluate the publish-surface "
        "projection this test is the closure evidence for."
    )


def test_no_row_is_rooted_at_coordinator_scripts():
    """No row's source_subdir publishes `coordinator/scripts/` (or a subpath of
    it) wholesale -- the three Step Zero scripts have no mirror row of their
    own."""
    rows = _parse_portable_rows(_PORTABLE_TARGETS_PATH)
    offending = [
        row["name"]
        for row in rows
        if row["source_subdir"] == _UNPUBLISHED_SOURCE_SUBDIR
        or row["source_subdir"].startswith(_UNPUBLISHED_SOURCE_SUBDIR + "/")
    ]
    assert not offending, (
        f"row(s) {offending} root a publish at {_UNPUBLISHED_SOURCE_SUBDIR} -- "
        "the C8 decision is authoring-only for chain-walk.py/normalize-env.py/"
        "install-maximalist.py; a row here means one of them is now reachable."
    )


def test_no_row_allowlists_a_step_zero_script_by_basename():
    """No row's allowlist names any of the three scripts by basename, from
    ANY source_subdir (closes the source_map indirection gap a
    source_subdir-only check would miss)."""
    rows = _parse_portable_rows(_PORTABLE_TARGETS_PATH)
    for script in _STEP_ZERO_SCRIPTS:
        offending = [
            row["name"]
            for row in rows
            if script in {entry.strip() for entry in row["allowlist"].split(",") if entry.strip()}
        ]
        assert not offending, (
            f"{script} is allowlisted by row(s) {offending} -- resolved INTO a named "
            "mirror, contradicting the C8 authoring-only decision."
        )


def test_top_level_scripts_row_allowlist_is_unwidened():
    """Pin `claude-klabauter-scripts` (source_subdir `scripts` -- this repo's
    TOP-LEVEL scripts/, scripts/setup.py's own directory, a different tree
    from coordinator/scripts/) to its known-correct three-entry allowlist.
    A future edit widening this allowlist to include any of the three Step
    Zero scripts would be the exact half-named-path bug this ledger item
    describes -- catch it here rather than downstream in a mirror diff."""
    rows = _parse_portable_rows(_PORTABLE_TARGETS_PATH)
    scripts_rows = [row for row in rows if row["name"] == "claude-klabauter-scripts"]
    assert len(scripts_rows) == 1, (
        "expected exactly one claude-klabauter-scripts row in "
        f"{_PORTABLE_TARGETS_PATH}, found {len(scripts_rows)}"
    )
    row = scripts_rows[0]
    assert row["source_subdir"] == "scripts"
    allowlist_entries = {entry.strip() for entry in row["allowlist"].split(",") if entry.strip()}
    assert allowlist_entries == {"setup.py", "setup.cmd", "test_setup.py"}
    for script in _STEP_ZERO_SCRIPTS:
        assert script not in allowlist_entries

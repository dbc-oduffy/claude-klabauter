"""test_percolate_parse_dryrun_deletion_gate.py — the deletion gate must
recognize the engine's real vocabulary, not a hand-written guess.

`_DELETING` (coordinator/bin/percolate-parse-dryrun.py) used to match
`\\b(deleting|del\\.)\\b` — a vocabulary that matches neither `DELETE` (what
the source memo believed the parser looked for) nor `REMOVE:`, the token
`publish_sync.py` actually prints (`publish_sync.py :: sync_mirror`,
`f"  REMOVE: {rel_path} (not in source)"`). A publish emitting real deletion
lines computed `step2_has_deletions: false`, silently skipping the /percolate
Step 3 PM-confirmation gate for an irreversible mirror deletion.

state/handoffs/2026-08-11-publish-path-gates-that-cannot-fire.md ties this to
`_has_deletions` -> `step2_has_deletions` -> `compute_gate_fire`'s
deletion-present OR-condition; this test pins `_has_deletions` alone against
a fixture built from `publish_sync.py`'s actual printed line, per the memo's
own instruction to avoid a second hand-written vocabulary.

NEGATIVE SPEC: does not exercise `compute_gate_fire` or the CLI end-to-end —
narrow to the regex/predicate this handoff named as broken.

Run: pytest coordinator/bin/tests/test_percolate_parse_dryrun_deletion_gate.py -q
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def parser():
    spec = importlib.util.spec_from_file_location(
        "_percolate_parse_dryrun_under_test", _BIN_DIR / "percolate-parse-dryrun.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_publish_sync_remove_line_is_recognized(parser):
    """Fixture copied verbatim from `publish_sync.py`'s own printed shape,
    not a hand-written guess at what it might print."""
    stdout_text = "  REMOVE: docs/stale-file.md (not in source)\n"
    assert parser._has_deletions(stdout_text) is True


def test_new_and_update_lines_alone_are_not_deletions(parser):
    stdout_text = "NEW: docs/added.md\nUPDATE: docs/changed.md\n"
    assert parser._has_deletions(stdout_text) is False


def test_stale_deleting_vocabulary_no_longer_matches_on_its_own(parser):
    """Guards against reintroducing the old `deleting`/`del.` guess as
    a second, drifting vocabulary alongside the real one."""
    stdout_text = "some unrelated line mentioning deleting nothing real\n"
    assert parser._has_deletions(stdout_text) is False
